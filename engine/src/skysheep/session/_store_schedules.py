"""SessionStore 的用户级日程（schedules）持久化方法。"""

from __future__ import annotations

import time


class SchedulesMixin:
    @staticmethod
    def _schedule_row(r) -> dict:
        return {
            "id": r["id"],
            "title": r["title"],
            "notes": r["notes"],
            "start_at": r["start_at"],
            # 0 = 未设结束时间（按点事件）；>0 时 start_at~end_at 是一个时间段
            "end_at": float(r["end_at"] or 0),
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
        end_at: float = 0,
    ) -> dict:
        assert self._db
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO schedules (title, notes, start_at, end_at, remind, remind_before,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                notes,
                start_at,
                max(0.0, float(end_at or 0)),
                1 if remind else 0,
                max(0, int(remind_before)),
                now,
                now,
            ),
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
        end_at: float | None = None,
        remind: bool | None = None,
        remind_before: int | None = None,
        done: bool | None = None,
    ) -> dict | None:
        """按传入字段部分更新；start_at 变化时重置已提醒标记（新时间要重新提醒）。

        end_at 传 0（或 None 以外的零值）即清除结束时间，回到按点事件。
        """
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
        if end_at is not None:
            sets.append("end_at = ?")
            args.append(max(0.0, float(end_at or 0)))
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
