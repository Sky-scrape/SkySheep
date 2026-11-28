"""SessionStore 的项目、项目任务清单、记忆地图演化摘要与团队频道消息持久化方法。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import aiosqlite

from ..messages import Message
from ._store_types import Project, project_from_row


class ProjectsMixin:
    async def get_remote_project(self) -> Project | None:
        """只读查询固定的「远程连接」项目：不存在返回 None（不创建）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM projects WHERE root_path = ?", (self.REMOTE_PROJECT_PATH,)
        )
        row = await cur.fetchone()
        if row:
            return project_from_row(row)
        return None

    async def ensure_remote_project(self) -> Project:
        """取（或建）固定的「远程连接」项目：飞书/微信等渠道的对话都归到它名下。"""
        assert self._db
        for _ in range(3):
            proj = await self.get_remote_project()
            if proj:
                if proj.removed_at > 0:
                    await self.restore_project(proj.id)
                    proj = await self.get_remote_project() or proj
                return proj
            try:
                await self._db.execute(
                    "INSERT INTO projects (root_path, name, created_at) VALUES (?, ?, ?)",
                    (self.REMOTE_PROJECT_PATH, "远程连接", time.time()),
                )
                await self._db.commit()
            except sqlite3.IntegrityError:
                await self._db.rollback()
        proj = await self.get_remote_project()
        if proj:
            return proj
        raise RuntimeError("远程连接项目多次建查仍未就绪")

    async def get_or_create_project(self, root_path: str, name: str = "") -> Project:
        assert self._db
        root_path = str(Path(root_path).resolve())
        for _ in range(3):
            cur = await self._db.execute(
                "SELECT * FROM projects WHERE root_path = ?", (root_path,)
            )
            row = await cur.fetchone()
            if row:
                proj = project_from_row(row)
                if proj.removed_at > 0:
                    await self.restore_project(proj.id)
                    proj = await self.get_project(proj.id) or proj
                return proj
            now = time.time()
            try:
                cur = await self._db.execute(
                    "INSERT INTO projects (root_path, name, created_at) VALUES (?, ?, ?)",
                    (root_path, name or Path(root_path).name, now),
                )
                await self._db.commit()
                return Project(cur.lastrowid, root_path, name or Path(root_path).name, now)
            except sqlite3.IntegrityError:
                await self._db.rollback()
        cur = await self._db.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        )
        row = await cur.fetchone()
        if row:
            return project_from_row(row)
        raise RuntimeError(f"无法创建或获取项目：{root_path}")

    async def list_projects(self, *, include_removed: bool = False) -> list[Project]:
        """项目列表；默认不含软移除（removed_at > 0）的项目。"""
        assert self._db
        where = "" if include_removed else " WHERE removed_at = 0"
        cur = await self._db.execute(
            "SELECT * FROM projects" + where + " ORDER BY created_at DESC, id DESC"
        )
        rows = await cur.fetchall()
        return [project_from_row(r) for r in rows]

    async def hide_project(self, project_id: int) -> bool:
        """软移除：只打 removed_at 标记，不删项目行、会话、白名单或任务。"""
        assert self._db
        cur = await self._db.execute(
            "UPDATE projects SET removed_at = ? WHERE id = ? AND removed_at = 0",
            (time.time(), int(project_id)),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def restore_project(self, project_id: int) -> bool:
        """恢复软移除的项目（removed_at 归零）。"""
        assert self._db
        cur = await self._db.execute(
            "UPDATE projects SET removed_at = 0 WHERE id = ? AND removed_at > 0",
            (int(project_id),),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def project_admin_list(self) -> list[dict]:
        """设置·项目页的数据：项目 + 会话数 + 最后活动时间（含已移除）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT p.*, COUNT(s.id) AS session_count,"
            " COALESCE(MAX(s.updated_at), p.created_at) AS last_active"
            " FROM projects p LEFT JOIN sessions s ON s.project_id = p.id"
            " GROUP BY p.id ORDER BY last_active DESC, p.id DESC"
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            p = project_from_row(r)
            out.append({
                "id": p.id, "name": p.name, "root_path": p.root_path,
                "created_at": p.created_at, "removed_at": p.removed_at,
                "session_count": int(r["session_count"] or 0),
                "last_active": float(r["last_active"] or 0),
            })
        return out

    async def get_project(self, project_id: int) -> Project | None:
        """按 id 取项目记录（定时任务按自身项目执行时解析工作目录用）。"""
        assert self._db
        cur = await self._db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cur.fetchone()
        return project_from_row(row) if row else None

    async def delete_project(self, project_id: int) -> int:
        """删除项目记录及其全部会话、白名单规则、任务清单与定时任务/流水线。"""
        assert self._db

        async def txn(conn: aiosqlite.Connection) -> int:
            await self._fts_delete(
                conn,
                "session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
                (project_id,),
            )
            await conn.execute(
                "DELETE FROM messages WHERE session_id IN "
                "(SELECT id FROM sessions WHERE project_id = ?)",
                (project_id,),
            )
            await conn.execute("DELETE FROM sessions WHERE project_id = ?", (project_id,))
            await conn.execute("DELETE FROM whitelist_rules WHERE project_id = ?", (project_id,))
            await conn.execute("DELETE FROM project_tasks WHERE project_id = ?", (project_id,))
            await conn.execute(
                "DELETE FROM pipeline_nodes WHERE pipeline_id IN "
                "(SELECT id FROM pipelines WHERE project_id = ?)",
                (project_id,),
            )
            await conn.execute("DELETE FROM pipelines WHERE project_id = ?", (project_id,))
            await conn.execute("DELETE FROM cron_tasks WHERE project_id = ?", (project_id,))
            await conn.execute("DELETE FROM map_digests WHERE project_id = ?", (project_id,))
            cur = await conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return cur.rowcount

        return await self._write_txn(txn)

    # ---- 项目任务清单 ----

    @staticmethod
    def _project_task_row(row) -> dict:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "detail": row["detail"],
            "done": bool(row["done"]),
            "created_at": row["created_at"],
            "done_at": row["done_at"],
        }

    async def add_project_task(self, project_id: int, title: str, detail: str = "") -> dict:
        assert self._db
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO project_tasks (project_id, title, detail, created_at) VALUES (?, ?, ?, ?)",
            (project_id, title, detail, now),
        )
        await self._db.commit()
        row = await self.get_project_task(cur.lastrowid)  # type: ignore[arg-type]
        assert row is not None
        return row

    async def get_project_task(self, task_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM project_tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return self._project_task_row(row) if row else None

    async def list_project_tasks(self, project_id: int) -> list[dict]:
        """未完成在前（按加入顺序，像清单步骤），已完成垫底（按完成时间倒序）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM project_tasks WHERE project_id = ? "
            "ORDER BY done ASC, CASE WHEN done = 0 THEN id ELSE -done_at END",
            (project_id,),
        )
        rows = await cur.fetchall()
        return [self._project_task_row(r) for r in rows]

    async def update_project_task(
        self, task_id: int, *, title: str | None = None, detail: str | None = None,
        done: bool | None = None,
    ) -> dict | None:
        """按传入字段部分更新；勾掉/重开时刷新 done_at。"""
        assert self._db
        row = await self.get_project_task(task_id)
        if not row:
            return None
        sets: list[str] = []
        args: list = []
        if title is not None:
            sets.append("title = ?")
            args.append(title)
        if detail is not None:
            sets.append("detail = ?")
            args.append(detail)
        if done is not None:
            sets.append("done = ?")
            args.append(1 if done else 0)
            sets.append("done_at = ?")
            args.append(time.time() if done else 0)
        if sets:
            args.append(task_id)
            await self._db.execute(
                f"UPDATE project_tasks SET {', '.join(sets)} WHERE id = ?", args
            )
            await self._db.commit()
        return await self.get_project_task(task_id)

    async def delete_project_task(self, task_id: int) -> int:
        assert self._db
        cur = await self._db.execute("DELETE FROM project_tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return cur.rowcount

    # ---- 记忆地图：演化摘要（map_digests）与项目演化时间线聚合 ----

    @staticmethod
    def _map_digest_row(row) -> dict:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "title": row["title"],
            "summary": row["summary"],
            "highlights": json.loads(row["highlights"] or "[]"),
            "topics": json.loads(row["topics"] or "[]"),
            "session_ids": json.loads(row["session_ids"] or "[]"),
            "start_ts": row["start_ts"],
            "end_ts": row["end_ts"],
            "created_at": row["created_at"],
        }

    async def list_map_digests(self, project_id: int) -> list[dict]:
        """某项目的演化摘要；overview 行排最前，其余按阶段起始时间升序。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM map_digests WHERE project_id = ?"
            " ORDER BY CASE WHEN kind = 'overview' THEN 0 ELSE 1 END, start_ts ASC",
            (project_id,),
        )
        rows = await cur.fetchall()
        return [self._map_digest_row(r) for r in rows]

    async def latest_map_digest_ts(self, project_id: int) -> float:
        """最近一次生成演化摘要的时间（没有则 0）；自动巡检的「冷却期」用。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT MAX(created_at) FROM map_digests WHERE project_id = ?", (project_id,)
        )
        row = await cur.fetchone()
        return float(row[0] or 0) if row else 0.0

    async def count_project_sessions_since(self, project_id: int, since_ts: float) -> int:
        """某项目自某时刻以来新建的会话数（自动生成演化摘要的触发阈值用）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_id = ? AND created_at > ?",
            (project_id, since_ts),
        )
        row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def replace_map_digests(self, project_id: int, rows: list[dict]) -> None:
        """整体替换某项目的演化摘要（独立写事务内先删后插，成功一次提交）。"""
        assert self._db
        now = time.time()

        async def txn(conn: aiosqlite.Connection) -> None:
            await conn.execute("DELETE FROM map_digests WHERE project_id = ?", (project_id,))
            for r in rows:
                await conn.execute(
                    "INSERT INTO map_digests (project_id, kind, title, summary, highlights,"
                    " topics, session_ids, start_ts, end_ts, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        "overview" if r.get("kind") == "overview" else "phase",
                        str(r.get("title") or "")[:200],
                        str(r.get("summary") or "")[:2000],
                        json.dumps(
                            [str(x)[:200] for x in (r.get("highlights") or [])[:10]],
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            [str(x)[:40] for x in (r.get("topics") or [])[:12]],
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            [str(x) for x in (r.get("session_ids") or [])[:500]],
                            ensure_ascii=False,
                        ),
                        float(r.get("start_ts") or 0),
                        float(r.get("end_ts") or 0),
                        now,
                    ),
                )

        await self._write_txn(txn)

    async def map_sessions_with_stats(
        self, project_id: int, start_ts: float, end_ts: float,
        *, limit: int | None = None,
    ) -> list[dict]:
        """记忆地图时间线的会话主体：窗口内的会话（含已归档）带消息数与 token 量。"""
        assert self._db
        if limit is not None:
            base_sql = (
                "SELECT id, title, summary, tags, pinned, archived, created_at, updated_at"
                " FROM sessions WHERE project_id = ? AND created_at >= ? AND created_at <= ?"
                " ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple = (project_id, start_ts, end_ts, max(1, int(limit)))
        else:
            base_sql = (
                "SELECT id, title, summary, tags, pinned, archived, created_at, updated_at"
                " FROM sessions WHERE project_id = ? AND created_at >= ? AND created_at <= ?"
            )
            params = (project_id, start_ts, end_ts)
        sql = (
            "SELECT t.id, t.title, t.summary, t.tags, t.pinned, t.archived,"
            " t.created_at, t.updated_at,"
            " (SELECT COUNT(*) FROM messages m WHERE m.session_id = t.id) AS msg_count,"
            " COALESCE((SELECT SUM(u.in_tokens) FROM usage_log u"
            " WHERE u.session_id = t.id), 0) AS in_tokens,"
            " COALESCE((SELECT SUM(u.out_tokens) FROM usage_log u"
            " WHERE u.session_id = t.id), 0) AS out_tokens"
            f" FROM ({base_sql}) t ORDER BY t.created_at ASC"
        )
        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        return [
            {
                "id": r["id"], "title": r["title"], "summary": r["summary"],
                "tags": [t for t in str(r["tags"] or "").split(",") if t],
                "pinned": bool(r["pinned"]), "archived": bool(r["archived"]),
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "msg_count": int(r["msg_count"]),
                "in_tokens": int(r["in_tokens"]), "out_tokens": int(r["out_tokens"]),
            }
            for r in rows
        ]

    async def map_day_stats(
        self, project_id: int, start_ts: float, end_ts: float,
    ) -> list[dict]:
        """按天聚合（本地时区）：活跃会话数与 token 消耗，热力图数据源。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT day, MAX(sc) AS sessions, MAX(tk) AS tokens FROM ("
            "  SELECT date(u.ts, 'unixepoch', 'localtime') AS day, 0 AS sc,"
            "    SUM(u.in_tokens + u.out_tokens) AS tk"
            "  FROM usage_log u JOIN sessions s ON s.id = u.session_id"
            "  WHERE s.project_id = ? AND u.ts >= ? AND u.ts <= ?"
            "  GROUP BY day"
            "  UNION"
            "  SELECT date(s.created_at, 'unixepoch', 'localtime'), COUNT(*), 0"
            "  FROM sessions s"
            "  WHERE s.project_id = ? AND s.created_at >= ? AND s.created_at <= ?"
            "  GROUP BY 1"
            ") GROUP BY day ORDER BY day ASC",
            (project_id, start_ts, end_ts, project_id, start_ts, end_ts),
        )
        return [
            {"day": r["day"], "sessions": int(r["sessions"]), "tokens": int(r["tokens"] or 0)}
            for r in await cur.fetchall()
        ]

    async def map_project_events(
        self, project_id: int, start_ts: float, end_ts: float,
    ) -> list[dict]:
        """项目演化事件：任务清单/流水线/定时任务的创建与完成时刻，时间升序。"""
        assert self._db
        events: list[dict] = []
        cur = await self._db.execute(
            "SELECT id, title, done, created_at, done_at FROM project_tasks"
            " WHERE project_id = ? AND ((created_at >= ? AND created_at <= ?)"
            "   OR (done != 0 AND done_at >= ? AND done_at <= ?))",
            (project_id, start_ts, end_ts, start_ts, end_ts),
        )
        for r in await cur.fetchall():
            if start_ts <= r["created_at"] <= end_ts:
                events.append({"ts": r["created_at"], "kind": "task_created",
                               "title": r["title"], "ref": r["id"]})
            if r["done"] and start_ts <= r["done_at"] <= end_ts:
                events.append({"ts": r["done_at"], "kind": "task_done",
                               "title": r["title"], "ref": r["id"]})
        cur = await self._db.execute(
            "SELECT id, name, status, finished_at FROM pipelines"
            " WHERE project_id = ? AND finished_at > 0 AND finished_at >= ? AND finished_at <= ?",
            (project_id, start_ts, end_ts),
        )
        for r in await cur.fetchall():
            events.append({"ts": r["finished_at"], "kind": "pipeline",
                           "title": r["name"], "ref": r["id"]})
        cur = await self._db.execute(
            "SELECT id, name, last_run_at FROM cron_tasks"
            " WHERE project_id = ? AND last_run_at > 0 AND last_run_at >= ? AND last_run_at <= ?",
            (project_id, start_ts, end_ts),
        )
        for r in await cur.fetchall():
            events.append({"ts": r["last_run_at"], "kind": "cron",
                           "title": r["name"], "ref": r["id"]})
        events.sort(key=lambda e: e["ts"])
        return events

    # ---- 团队频道消息（team_messages，三期） ----

    @staticmethod
    def _team_message_row(row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "session_id": row["session_id"],
            "seq": row["seq"],
            "from_member": row["from_member"],
            "to_member": row["to_member"],
            "msg_kind": row["msg_kind"],
            "task_ref": row["task_ref"],
            "text": row["text"],
            "created_at": row["created_at"],
        }

    async def add_team_message(
        self, team_id: str, seq: int, from_member: str, to_member: str,
        msg_kind: str = "system", task_ref: str = "", text: str = "",
        session_id: str = "", created_at: float | None = None,
    ) -> int:
        """团队频道消息落库一行（三期）：编排器的持久化钩子逐条定稿时调用。"""
        assert self._db
        cur = await self._db.execute(
            "INSERT INTO team_messages (team_id, session_id, seq, from_member,"
            " to_member, msg_kind, task_ref, text, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, session_id, int(seq), from_member, to_member,
             msg_kind, task_ref, text,
             time.time() if created_at is None else float(created_at)),
        )
        await self._db.commit()
        return cur.lastrowid

    async def list_team_messages(self, team_id: str) -> list[dict]:
        """某团队的全部频道消息，seq 升序（team.log 回放；id 作稳定次级键）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM team_messages WHERE team_id = ? ORDER BY seq ASC, id ASC",
            (team_id,),
        )
        rows = await cur.fetchall()
        return [self._team_message_row(r) for r in rows]

    async def last_team_message_seq(self, team_id: str) -> int:
        """某团队频道消息的最大 seq（常驻团队复用的续号水位；查无回 0）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT MAX(seq) FROM team_messages WHERE team_id = ?",
            (team_id,),
        )
        row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def last_team_board_tasks(
        self, session_id: str, team_id: str = ""
    ) -> list[dict]:
        """该会话最后一份团队 meta 的工单板（team.tasks；没有团队 meta 回 []）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT content FROM messages"
            " WHERE session_id = ? AND role = 'assistant'"
            " AND content LIKE '%\"mode\":\"team\"%'"
            " ORDER BY seq DESC LIMIT 20",
            (session_id,),
        )
        for row in await cur.fetchall():
            try:
                msg = Message.model_validate_json(row["content"])
            except Exception:  # noqa: BLE001
                continue
            meta = msg.roundtable or {}
            if meta.get("mode") != "team":
                continue
            team = meta.get("team") or {}
            if team_id and str(team.get("team_id") or "") != team_id:
                return []
            tasks = team.get("tasks")
            if isinstance(tasks, list):
                return [t for t in tasks if isinstance(t, dict)]
            return []
        return []
