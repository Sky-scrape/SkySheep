"""SessionStore 的白名单规则、渠道绑定、用量统计与存储空间清理方法。"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from ._store_types import _ALL

logger = logging.getLogger("skysheep.store")


class UsageAdminMixin:
    # 可清理表清单：(表名, 时间列, 界面标签)。只收「只进不出」的账本类数据。
    CLEANABLE_TABLES: tuple[tuple[str, str, str], ...] = (
        ("usage_log", "ts", "用量记录（token 消耗历史）"),
        ("team_messages", "created_at", "团队频道消息"),
        ("channel_sources", "last_seen", "远程连接来源记录"),
        ("map_digests", "start_ts", "项目地图摘要"),
    )

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
        cur = await self._db.execute(
            "SELECT id, tool, kind, pattern, created_at, enabled, hit_count, last_hit_at"
            " FROM whitelist_rules WHERE project_id = ? ORDER BY id DESC",
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
                "enabled": bool(r["enabled"]),
                "hit_count": r["hit_count"],
                "last_hit_at": r["last_hit_at"],
            }
            for r in rows
        ]

    async def set_rule_enabled(
        self, rule_id: int, project_id: int, enabled: bool,
    ) -> None:
        """启停一条白名单规则。归属条件写进 UPDATE。"""
        assert self._db
        cur = await self._db.execute(
            "UPDATE whitelist_rules SET enabled = ? WHERE id = ? AND project_id = ?",
            (1 if enabled else 0, rule_id, project_id),
        )
        await self._db.commit()
        if not cur.rowcount:
            raise KeyError("rule not found in this project")

    async def record_rule_hit(self, rule_id: int) -> None:
        """白名单命中记账：次数 +1、刷新最近命中时间。"""
        assert self._db
        await self._db.execute(
            "UPDATE whitelist_rules SET hit_count = hit_count + 1, last_hit_at = ?"
            " WHERE id = ?",
            (time.time(), rule_id),
        )
        await self._db.commit()

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

    async def remove_rule(self, rule_id: int, project_id: int | None = None) -> None:
        """删除一条白名单规则；给出 project_id 时只删该项目的规则。"""
        assert self._db
        if project_id is None:
            await self._db.execute("DELETE FROM whitelist_rules WHERE id = ?", (rule_id,))
        else:
            await self._db.execute(
                "DELETE FROM whitelist_rules WHERE id = ? AND project_id = ?",
                (rule_id, project_id),
            )
        await self._db.commit()

    # ---- 聊天软件渠道（Bot Channel） ----

    async def get_channel_binding(self, channel: str) -> str | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT session_id FROM channel_bindings WHERE channel = ?", (channel,)
        )
        row = await cur.fetchone()
        return row["session_id"] if row else None

    async def count_channel_bindings(self, channel: str) -> int:
        assert self._db
        cur = await self._db.execute(
            "SELECT COUNT(*) AS n FROM channel_bindings"
            " WHERE channel = ? OR channel LIKE ? ESCAPE '\\'",
            (channel, channel.replace("\\", "\\\\").replace("%", "\\%")
             .replace("_", "\\_") + "#%"),
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def set_channel_binding(self, channel: str, session_id: str) -> None:
        assert self._db
        await self._db.execute(
            "INSERT INTO channel_bindings (channel, session_id, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(channel) DO UPDATE SET session_id = excluded.session_id,"
            " updated_at = excluded.updated_at",
            (channel, session_id, time.time()),
        )
        await self._db.commit()

    async def find_channel_by_session(self, session_id: str) -> str | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT channel FROM channel_bindings WHERE session_id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return row["channel"] if row else None

    async def record_channel_source(self, channel: str, chat_id: str, actor: str = "") -> None:
        assert self._db
        now = time.time()
        await self._db.execute(
            "INSERT INTO channel_sources (channel, chat_id, actor, first_seen, last_seen, hits)"
            " VALUES (?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(channel, chat_id) DO UPDATE SET"
            " actor = excluded.actor, last_seen = excluded.last_seen, hits = hits + 1",
            (channel, chat_id, actor, now, now),
        )
        await self._db.commit()

    async def list_channel_sources(self, channel: str | None = None) -> list[dict]:
        assert self._db
        if channel:
            cur = await self._db.execute(
                "SELECT * FROM channel_sources WHERE channel = ? ORDER BY last_seen DESC",
                (channel,),
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM channel_sources ORDER BY last_seen DESC"
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---- 用量统计（usage_log） ----

    async def add_usage(
        self, session_id: str, provider: str, model: str,
        in_tokens: int, out_tokens: int, cached_tokens: int = 0,
        *, tool_calls: int = 0, tool_errors: int = 0, failed: int = 0,
    ) -> None:
        """记一轮（或一次协作调用）的用量。"""
        if in_tokens <= 0 and out_tokens <= 0:
            return
        assert self._db
        await self._db.execute(
            "INSERT INTO usage_log (session_id, provider, model, ts, in_tokens, out_tokens,"
            " cached_tokens, tool_calls, tool_errors, failed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, provider, model, time.time(), in_tokens, out_tokens, max(0, cached_tokens),
             max(0, int(tool_calls)), max(0, int(tool_errors)), 1 if failed else 0),
        )
        await self._db.commit()

    # ---- 存储占用与表级清理 ----

    async def table_counts(self) -> dict[str, int]:
        """各可清理表 + 核心表的行数快照（设置页展示用）。"""
        assert self._db
        out: dict[str, int] = {}
        allowed = {t for t, _, _ in self.CLEANABLE_TABLES} | {"projects", "sessions", "messages"}
        for name in sorted(allowed):
            cur = await self._db.execute(f"SELECT COUNT(*) FROM {name}")
            row = await cur.fetchone()
            out[name] = int(row[0] or 0) if row else 0
        return out

    async def cleanup_tables(self, targets: list[dict]) -> dict:
        """按表名 + 保留天数删掉更早的账本行，返回 `{表名: 删掉行数}`。"""
        assert self._db
        allowed = {name: col for name, col, _ in self.CLEANABLE_TABLES}
        deleted: dict[str, int] = {}
        for item in targets:
            table = str(item.get("table") or "")
            col = allowed.get(table)
            if col is None:
                continue
            try:
                days = int(item.get("days") or 0)
            except (TypeError, ValueError):
                days = 0
            if days > 0:
                cutoff = time.time() - days * 86400
                cur = await self._db.execute(
                    f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)
                )
            else:
                cur = await self._db.execute(f"DELETE FROM {table}")
            deleted[table] = int(cur.rowcount or 0)
        await self._db.commit()
        return deleted

    async def vacuum(self) -> None:
        """整理数据库文件，释放已删数据占用的空间（VACUUM）。"""
        assert self._db
        await self._db.execute("VACUUM")
        await self._db.commit()

    def db_size(self) -> int:
        """主库文件字节数（不含 WAL/SHM；不存在时 0）。"""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    async def usage_stats(self, days: int = 14, *, project_id: int | None | object = _ALL) -> dict:
        """按天聚合 + 按会话聚合 + 按服务×模型聚合（近 N 天）。日期用本地时区。"""
        assert self._db
        since = time.time() - days * 86400
        scoped = project_id is not _ALL
        by_day, by_session, by_provider = [], [], []
        session_count = 0
        streak_current = streak_longest = 0
        longest_chat = 0.0
        try:
            join = (
                " FROM usage_log l JOIN sessions s ON s.id = l.session_id"
                if scoped else " FROM usage_log l"
            )
            cond = " WHERE l.ts >= ?" + (" AND s.project_id IS ?" if scoped else "")
            args: tuple = (since, project_id) if scoped else (since,)
            cur = await self._db.execute(
                "SELECT date(l.ts, 'unixepoch', 'localtime') AS day,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot"
                + join + cond + " GROUP BY day ORDER BY day DESC LIMIT ?",
                (*args, days),
            )
            by_day = [dict(r) for r in await cur.fetchall()]
            cur = await self._db.execute(
                "SELECT COUNT(DISTINCT session_id) AS c" + join + cond,
                args,
            )
            row = await cur.fetchone()
            session_count = int(row[0] or 0) if row else 0
            cur = await self._db.execute(
                "SELECT l.session_id AS sid, MAX(s.title) AS title,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot, MAX(l.ts) AS last_ts"
                " FROM usage_log l LEFT JOIN sessions s ON s.id = l.session_id"
                " WHERE l.ts >= ?" + (" AND s.project_id IS ?" if scoped else "")
                + " GROUP BY l.session_id"
                " ORDER BY last_ts DESC LIMIT 12",
                args,
            )
            by_session = [dict(r) for r in await cur.fetchall()]
            cur = await self._db.execute(
                "SELECT l.provider AS provider, l.model AS model,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot,"
                " SUM(l.cached_tokens) AS cached"
                + join + cond + " GROUP BY l.provider, l.model",
                args,
            )
            by_provider = [dict(r) for r in await cur.fetchall()]
            cur = await self._db.execute(
                "SELECT COUNT(*) AS turns, COALESCE(SUM(l.failed), 0) AS failed,"
                " COALESCE(SUM(l.tool_calls), 0) AS tc, COALESCE(SUM(l.tool_errors), 0) AS te"
                + join + cond,
                args,
            )
            row = await cur.fetchone()
            if row and int(row[0] or 0) > 0:
                turns_total = int(row[0])
                turns_failed = int(row[1] or 0)
                tool_calls_sum = int(row[2] or 0)
                tool_errors_sum = int(row[3] or 0)
                quality = {
                    "turns": turns_total,
                    "turns_failed": turns_failed,
                    "turn_success_rate": round(1.0 - turns_failed / turns_total, 4),
                    "tool_calls": tool_calls_sum,
                    "tool_errors": tool_errors_sum,
                    "tool_error_rate": (
                        round(tool_errors_sum / tool_calls_sum, 4) if tool_calls_sum > 0 else None
                    ),
                }
            else:
                quality = None
            streak_current, streak_longest = await self._usage_streaks(scoped, project_id)
            longest_chat = await self._longest_chat_span(scoped, project_id)
        except Exception:  # noqa: BLE001
            quality = None
        return {
            "by_day": by_day,
            "by_session": by_session,
            "by_provider": by_provider,
            "session_count": session_count,
            "streak_current": streak_current,
            "streak_longest": streak_longest,
            "longest_chat_seconds": longest_chat,
            "quality": quality,
        }

    async def _usage_streaks(self, scoped: bool, project_id) -> tuple[int, int]:
        """按「有用量记录的自然日」算当前连续天数与历史最长连续天数。"""
        assert self._db
        if scoped:
            cur = await self._db.execute(
                "SELECT DISTINCT date(l.ts, 'unixepoch', 'localtime') AS day"
                " FROM usage_log l JOIN sessions s ON s.id = l.session_id"
                " WHERE s.project_id IS ? ORDER BY day ASC",
                (project_id,),
            )
        else:
            cur = await self._db.execute(
                "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') AS day"
                " FROM usage_log ORDER BY day ASC"
            )
        days = [str(r["day"]) for r in await cur.fetchall() if r["day"]]
        if not days:
            return 0, 0
        day_set = set(days)
        longest = run = 1
        prev = date.fromisoformat(days[0])
        for raw in days[1:]:
            cur_day = date.fromisoformat(raw)
            run = run + 1 if (cur_day - prev).days == 1 else 1
            longest = max(longest, run)
            prev = cur_day
        anchor = date.today()
        if anchor.isoformat() not in day_set:
            anchor -= timedelta(days=1)
        current = 0
        while anchor.isoformat() in day_set:
            current += 1
            anchor -= timedelta(days=1)
        return current, longest

    async def _longest_chat_span(self, scoped: bool, project_id) -> float:
        """最长一次会话的跨度（首条→末条消息的秒数）；无消息/无归属返回 0。"""
        assert self._db
        where = " WHERE s.project_id IS ?" if scoped else ""
        args = (project_id,) if scoped else ()
        cur = await self._db.execute(
            "SELECT MAX(m.created_at) - MIN(m.created_at) AS span"
            " FROM messages m JOIN sessions s ON s.id = m.session_id"
            + where + " GROUP BY m.session_id ORDER BY span DESC LIMIT 1",
            args,
        )
        row = await cur.fetchone()
        return float(row["span"] or 0) if row and row["span"] else 0.0

    async def usage_today(self) -> int | None:
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
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "usage_today 查询失败，当日用量按未知（None）上报：%s", e,
            )
            return None
