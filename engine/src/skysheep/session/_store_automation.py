"""SessionStore 的定时任务、自动化运行账本（factory）与流水线方法。"""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite


class AutomationStoreMixin:
    # 与设计稿 §8.1 的截断口径一致：结果摘要 ≤200 字、错误摘要 ≤300 字
    MAX_RESULT_EXCERPT_CHARS = 200
    MAX_RUN_ERROR_CHARS = 300

    # 流水线规模上限：并发调度兜住的是「同时跑多少」，这里兜的是「一共排多少」——
    # 没有限制时一条几千节点的流水线会让列表渲染和每 5 秒的扫描都变慢
    MAX_PIPELINE_NODES = 50
    MAX_NODE_PROMPT_CHARS = 10_000
    MAX_NODE_TITLE_CHARS = 200
    MAX_PIPELINE_NAME_CHARS = 100

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
    def _parse_allowed_tools(raw_tools: object) -> list[str]:
        s = str(raw_tools or "").strip()
        if not s or s == "[]":
            return []
        if s.startswith("["):
            try:
                loaded = json.loads(s)
                if isinstance(loaded, list):
                    return [str(t).strip() for t in loaded if str(t).strip()]
            except ValueError:
                pass
        return [t.strip() for t in s.split(",") if t.strip()]

    @classmethod
    def _cron_row(cls, r) -> dict:
        keys = r.keys()
        mode = r["mode"] if "mode" in keys else "agent"
        return {
            "id": r["id"],
            "project_id": r["project_id"],
            "name": r["name"],
            "prompt": r["prompt"],
            "schedule_type": r["schedule_type"],
            "interval_minutes": r["interval_minutes"],
            "time_of_day": r["time_of_day"],
            "weekday": r["weekday"],
            "allowed_tools": cls._parse_allowed_tools(r["allowed_tools"]),
            "enabled": bool(r["enabled"]),
            "notify_channel": bool(r["notify_channel"]),
            # 任务形态（定时团队）：mode 原值 + is_team bool（前端按它分流渲染）
            "mode": mode,
            "is_team": mode == "team",
            "team_template": r["team_template"] if "team_template" in keys else "",
            "provider_name": r["provider_name"] if "provider_name" in keys else "",
            "model": r["model"] if "model" in keys else "",
            "review": bool(r["review"]) if "review" in keys else False,
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
        notify_channel: bool = False,
        mode: str = "agent",
        team_template: str = "",
        provider_name: str = "",
        model: str = "",
        review: bool = False,
    ) -> dict:
        assert self._db
        tools = ",".join(allowed_tools or [])
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO cron_tasks (project_id, name, prompt, schedule_type, interval_minutes,"
            " time_of_day, weekday, allowed_tools, notify_channel, mode, team_template,"
            " provider_name, model, review, created_at, next_run_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                name,
                prompt,
                schedule_type,
                interval_minutes,
                time_of_day,
                weekday,
                tools,
                1 if notify_channel else 0,
                mode,
                team_template,
                provider_name,
                model,
                1 if review else 0,
                now,
                0.0,
            ),
        )
        await self._db.commit()
        return await self.get_cron_task(cur.lastrowid)

    @staticmethod
    def _build_updates(
        kw: dict[str, Any],
        allowed: tuple[str, ...],
        transforms: dict[str, Any] | None = None,
    ) -> tuple[list[str], list[Any]]:
        fields: list[str] = []
        args: list[Any] = []
        tf = transforms or {}
        for k, v in kw.items():
            if k not in allowed:
                continue
            if k in tf:
                v = tf[k](v)
            fields.append(f"{k} = ?")
            args.append(v)
        return fields, args

    async def update_cron_task(self, task_id: int, **kw) -> dict | None:
        assert self._db
        allowed = (
            "name",
            "prompt",
            "schedule_type",
            "interval_minutes",
            "time_of_day",
            "weekday",
            "allowed_tools",
            "enabled",
            "notify_channel",
            "next_run_at",
            "last_run_at",
            "last_status",
            "last_result",
            "mode",
            "team_template",
            "provider_name",
            "model",
            "review",
        )
        fields, args = self._build_updates(
            kw, allowed, {"allowed_tools": lambda v: ",".join(v or [])}
        )
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

    async def claim_cron_task(
        self, task_id: int, expected_next_run_at: float, new_next_run_at: float
    ) -> bool:
        """CAS 认领一个到点的定时任务：next_run_at 仍等于期望值时原子前移。

        单条 UPDATE 带 WHERE 双条件（id + 旧 next_run_at），原子性由 SQLite
        写锁保证：应用内扫描循环与 cron-run 子进程（系统计划任务拉起）两个
        触发方各拿自己读到的旧值竞争，只有先到者改得动（rowcount=1 认领
        成功），后到者 0 行受影响即放弃——跨进程去重，杜绝同一到点槽位被
        两边各跑一轮（副作用重复、token 双倍、回写互覆）。

        认领值只是占位：执行完成/失败/取消的既有回写照常按真实完成时刻
        重排 next_run_at 覆盖它；认领方在执行中崩溃时占位值在未来，任务
        到点后自然恢复，不会卡死也不会立即重放。返回是否认领成功。
        """
        assert self._db
        cur = await self._db.execute(
            "UPDATE cron_tasks SET next_run_at = ? WHERE id = ? AND next_run_at = ?",
            (float(new_next_run_at), int(task_id), float(expected_next_run_at)),
        )
        await self._db.commit()
        return cur.rowcount > 0

    # ---- 运行账本（automation_runs）：每次自动化运行一行 ----
    # 既有表只保存每个任务的「最后一次」运行（last_run_at/last_status/last_result），
    # 没有历史与耗时；工厂观战需要「运行」这一实体。写入点见
    # core/run_ledger.py（RunLedger）与 server/backend_parts/automation.py。
    # 外部输入一律走占位符绑定；SQL 片段只拼接本方法内的常量字符串。

    @staticmethod
    def _automation_run_row(r) -> dict:
        return {
            "id": r["id"],
            "kind": r["kind"],
            "task_id": r["task_id"],
            "pipeline_id": r["pipeline_id"],
            "project_id": r["project_id"],
            "session_id": r["session_id"],
            "trigger": r["trigger"],
            "status": r["status"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "duration_s": r["duration_s"],
            "result_excerpt": r["result_excerpt"] or "",
            "error": r["error"] or "",
            "in_tokens": int(r["in_tokens"] or 0),
            "out_tokens": int(r["out_tokens"] or 0),
            "cached_tokens": int(r["cached_tokens"] or 0),
            "denied_count": int(r["denied_count"] or 0),
            "tool_errors": int(r["tool_errors"] or 0),
            "denied_tools": r["denied_tools"] or "",
            "error_kind": r["error_kind"] or "",
            "outcome": r["outcome"] or "",
            "outcome_source": r["outcome_source"] or "",
            "config_snapshot": r["config_snapshot"] or "",
            "review_note": r["review_note"] or "",
        }

    async def get_automation_run(self, run_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM automation_runs WHERE id = ?", (int(run_id),)
        )
        row = await cur.fetchone()
        return self._automation_run_row(row) if row else None

    async def clear_automation_run_session(self, session_id: str) -> int:
        """会话删除：断开工单运行账本里的会话链接，运行历史与统计保留。

        automation_runs 是工厂的长期账本（日报统计也读它），删会话不该把
        整行历史抹掉；但 session_id 指向已删会话就是死链（工厂抽屉的
        「打开会话」会开出一张空标签），所以只清链接，不删行。
        """
        if not session_id:
            return 0
        assert self._db
        cur = await self._db.execute(
            "UPDATE automation_runs SET session_id = NULL WHERE session_id = ?",
            (str(session_id),),
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def add_automation_run(
        self,
        *,
        kind: str,
        task_id: int,
        pipeline_id: int | None = None,
        project_id: int | None = None,
        session_id: str | None = None,
        trigger: str = "schedule",
        status: str = "running",
        started_at: float | None = None,
        config_snapshot: str = "",
    ) -> dict:
        """开一条运行记录；默认 status=running（结束请走 finish_automation_run）。

        config_snapshot 是开跑时产线配置的有界 JSON 快照（提示词 / 预授权 / 形态）；
        由 ``RunLedger`` 写入，旧行默认空串。
        """
        assert self._db
        ts = time.time() if started_at is None else float(started_at)
        cur = await self._db.execute(
            "INSERT INTO automation_runs (kind, task_id, pipeline_id, project_id,"
            " session_id, trigger, status, started_at, config_snapshot)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(kind),
                int(task_id),
                None if pipeline_id is None else int(pipeline_id),
                None if project_id is None else int(project_id),
                None if session_id is None else str(session_id),
                str(trigger),
                str(status),
                ts,
                str(config_snapshot or ""),
            ),
        )
        run_id = cur.lastrowid
        await self._db.commit()
        return await self.get_automation_run(run_id)

    async def finish_automation_run(
        self,
        run_id: int,
        *,
        status: str,
        finished_at: float | None = None,
        duration_s: float | None = None,
        result_excerpt: str = "",
        error: str = "",
        session_id: str | None = None,
        in_tokens: int = 0,
        out_tokens: int = 0,
        cached_tokens: int = 0,
        denied_count: int = 0,
        tool_errors: int = 0,
        denied_tools: str = "",
        error_kind: str = "",
        outcome: str = "",
        outcome_source: str = "",
        review_note: str = "",
    ) -> dict | None:
        """收尾一条运行；不存在返回 None。

        duration_s 缺省按 finished_at - started_at 计算（服务重启等场景由调用方
        显式传入时以调用方为准）。session_id 只覆盖不抹除：None = 保持原值。
        token 三项是同一次运行的用量快照（由账本按会话 + 时间窗汇总后传入），
        与状态一起写入，之后即使 usage_log 被清理也留得住。

        denied_count：本轮被无人值守门控拒绝的工具次数（0 = 没被拦）。这是
        「预授权工具没勾全」这类最常见故障的唯一痕迹。
        tool_errors：本轮工具报错次数（带病跑完的无效步骤信号，巡检消化）。
        denied_tools：被拦工具名 → 次数的 JSON 快照（只留前几项），用于直接
        告诉用户该补哪些预授权工具。
        error_kind：失败归因分类（permission / model / path / budget / unknown）。
        outcome / outcome_source：把「跑完」与「做成」分开——auto 为收尾时的
        机械判定（被拦过即 partial），user 为用户在运行详情里的手动标记。
        outcome 为空表示还没有结论（running / 跳过 / 叫停）。
        """
        assert self._db
        run = await self.get_automation_run(run_id)
        if run is None:
            return None
        fin = time.time() if finished_at is None else float(finished_at)
        if duration_s is None:
            duration = max(0.0, fin - float(run["started_at"] or fin))
        else:
            duration = max(0.0, float(duration_s))
        await self._db.execute(
            "UPDATE automation_runs SET status = ?, finished_at = ?, duration_s = ?,"
            " result_excerpt = ?, error = ?,"
            " session_id = COALESCE(?, session_id),"
            " in_tokens = ?, out_tokens = ?, cached_tokens = ?,"
            " denied_count = ?, tool_errors = ?, error_kind = ?,"
            " denied_tools = ?,"
            " outcome = CASE WHEN ? = '' THEN outcome ELSE ? END,"
            " outcome_source = CASE WHEN ? = '' THEN outcome_source ELSE ? END,"
            " review_note = ?"
            " WHERE id = ?",
            (
                str(status),
                fin,
                duration,
                str(result_excerpt or "")[: self.MAX_RESULT_EXCERPT_CHARS],
                str(error or "")[: self.MAX_RUN_ERROR_CHARS],
                None if session_id is None else str(session_id),
                max(0, int(in_tokens)),
                max(0, int(out_tokens)),
                max(0, int(cached_tokens)),
                max(0, int(denied_count)),
                max(0, int(tool_errors)),
                str(error_kind or "")[:24],
                str(denied_tools or "")[:600],
                str(outcome or ""),
                str(outcome or ""),
                str(outcome_source or ""),
                str(outcome_source or ""),
                str(review_note or "")[:200],
                int(run_id),
            ),
        )
        await self._db.commit()
        return await self.get_automation_run(run_id)

    async def set_automation_run_outcome(
        self,
        run_id: int,
        outcome: str,
        *,
        source: str = "user",
    ) -> dict | None:
        """人工改写一条运行的「做成」判定（'' 表示撤销标记，回到自动口径）。"""
        assert self._db
        await self._db.execute(
            "UPDATE automation_runs SET outcome = ?, outcome_source = ? WHERE id = ?",
            (str(outcome or ""), str(source or ""), int(run_id)),
        )
        await self._db.commit()
        return await self.get_automation_run(run_id)

    async def mark_interrupted_runs(self) -> int:
        """把残留的 running 运行统一标为 interrupted（应用启动时收尾）。

        只动 status，不动 finished_at 已有值——正常运行的 finished_at 本就为
        NULL，这里保持原样，由工厂页按 started_at 推算「中断于未知时刻」。
        """
        assert self._db
        cur = await self._db.execute(
            "UPDATE automation_runs SET status = 'interrupted' WHERE status = 'running'"
        )
        await self._db.commit()
        return max(0, int(cur.rowcount or 0))

    async def list_automation_runs(
        self,
        *,
        kind: str | None = None,
        project_id: int | None = None,
        task_id: int | None = None,
        pipeline_id: int | None = None,
        status: str | None = None,
        outcome: str | None = None,
        started_since: float | None = None,
        finished_since: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """运行历史分页（最近在前）。过滤条件全部参数绑定。"""
        assert self._db
        clauses: list[str] = []
        args: list = []
        for col, val, cast in (
            ("kind", kind, str),
            ("project_id", project_id, int),
            ("task_id", task_id, int),
            ("pipeline_id", pipeline_id, int),
            ("status", status, str),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                args.append(cast(val))
        if outcome == "ok_or_legacy":
            clauses.append("(outcome = 'ok' OR (outcome = '' AND status = 'done'))")
        elif outcome is not None:
            clauses.append("outcome = ?")
            args.append(str(outcome))
        if started_since is not None:
            clauses.append("started_at >= ?")
            args.append(float(started_since))
        if finished_since is not None:
            clauses.append("finished_at >= ?")
            args.append(float(finished_since))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        lim = max(1, min(500, int(limit)))
        off = max(0, int(offset))
        cur = await self._db.execute(
            f"SELECT * FROM automation_runs{where}"
            " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (*args, lim, off),
        )
        rows = await cur.fetchall()
        return [self._automation_run_row(r) for r in rows]

    async def prune_automation_runs(self, *, keep: int = 2000, days: int = 30) -> int:
        """按「最近 keep 条 + 最近 days 天」清理历史，返回删除条数。

        两个条件是并集：超出条数上限的，或早于天数窗口的，都清。running 行
        永不删（进行中的运行必须能被收尾，删了就成了没有归属的孤儿记录）。
        保留集按 started_at 倒序取前 keep 条——running 通常也在其中。
        """
        assert self._db
        keep = max(1, int(keep))
        days = max(1, int(days))
        cutoff = time.time() - days * 86400.0
        cur = await self._db.execute(
            "DELETE FROM automation_runs WHERE status != 'running' AND ("
            " started_at < ? OR id IN ("
            "  SELECT id FROM automation_runs ORDER BY started_at DESC, id DESC"
            "  LIMIT -1 OFFSET ?))",
            (cutoff, keep),
        )
        await self._db.commit()
        return max(0, int(cur.rowcount or 0))

    async def automation_run_counters(self, *, done_since: float) -> dict:
        """工厂计数聚合：running 总数与 done_since 之后的「做成」条数。

        单条 SQL 聚合，避免工厂页把全量历史拉回前端再数。「今日做成」按
        outcome 口径：`ok` 才算，`partial`（被拦过）不算；旧行没有 outcome
        时回落到 `status = done`，避免升级当天的历史凭空少一截。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT"
            " SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,"
            " SUM(CASE WHEN finished_at >= ? AND ("
            "   outcome = 'ok' OR (outcome = '' AND status = 'done'))"
            " THEN 1 ELSE 0 END) AS done_today,"
            " SUM(CASE WHEN started_at >= ? THEN in_tokens + out_tokens ELSE 0 END)"
            " AS tokens_today FROM automation_runs",
            (float(done_since), float(done_since)),
        )
        row = await cur.fetchone()
        return {
            "running": int((row["running"] if row else 0) or 0),
            "done_today": int((row["done_today"] if row else 0) or 0),
            "tokens_today": int((row["tokens_today"] if row else 0) or 0),
        }

    async def automation_runs_since(self, since: float, *, limit: int = 4000) -> list[dict]:
        """since 之后开始的全部运行（新→旧），供工厂算「需留意」与产线体检。

        与 ``list_automation_runs`` 的区别是**不按 60 条截断**：24h 内的失败
        被 500 条上限挡住会静默漏看，体检也要 7 天的样本。行数天然受
        ``prune_automation_runs`` 的保留策略约束（默认 2000 条），这里的
        limit 只是兜底，正常不会触发。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM automation_runs WHERE started_at >= ?"
            " ORDER BY started_at DESC, id DESC LIMIT ?",
            (float(since), max(1, min(20000, int(limit)))),
        )
        rows = await cur.fetchall()
        return [self._automation_run_row(r) for r in rows]

    async def session_usage_between(
        self,
        session_id: str,
        since: float,
        until: float | None = None,
    ) -> dict:
        """一次运行用掉的 token（按会话 + 时间窗汇总 usage_log）。

        定时任务每次运行都新建会话，所以会话 id 基本等价于「这一次运行」；
        会话续跑型编排节点复用会话，时间窗把范围收回到本次运行。
        """
        assert self._db
        clauses = ["session_id = ?", "ts >= ?"]
        args: list = [str(session_id), float(since)]
        if until is not None:
            clauses.append("ts <= ?")
            args.append(float(until))
        cur = await self._db.execute(
            "SELECT SUM(in_tokens) AS it, SUM(out_tokens) AS ot,"
            " SUM(cached_tokens) AS ct FROM usage_log WHERE " + " AND ".join(clauses),
            tuple(args),
        )
        row = await cur.fetchone()
        return {
            "in_tokens": int((row["it"] if row else 0) or 0),
            "out_tokens": int((row["ot"] if row else 0) or 0),
            "cached_tokens": int((row["ct"] if row else 0) or 0),
        }

    async def factory_ack(self, line_key: str, at: float | None = None) -> float:
        """把一条产线的「需留意」标记为已处理（同一产线只留最新一次）。"""
        assert self._db
        ts = time.time() if at is None else float(at)
        await self._db.execute(
            "INSERT INTO factory_acks (line_key, acked_at) VALUES (?, ?)"
            " ON CONFLICT(line_key) DO UPDATE SET acked_at = excluded.acked_at",
            (str(line_key), ts),
        )
        await self._db.commit()
        return ts

    async def list_factory_acks(self) -> dict[str, float]:
        """全部已处理的产线标记（line_key → 时间戳）。"""
        assert self._db
        cur = await self._db.execute("SELECT line_key, acked_at FROM factory_acks")
        rows = await cur.fetchall()
        return {str(r["line_key"]): float(r["acked_at"] or 0) for r in rows}

    async def factory_alert_claim(self, line_key: str, day: str) -> bool:
        """申领一次失败告警额度：同一条产线同一天只有第一次返回 True。

        告警是「吵醒用户」的动作，宁少勿多：连续失败聚合告警按天去重，
        一条产线一天最多一条。天变了自动重新申领成功（按行覆盖）。
        """
        assert self._db

        async def txn(conn: aiosqlite.Connection) -> bool:
            cur = await conn.execute(
                "SELECT day FROM factory_alerts WHERE line_key = ?", (str(line_key),)
            )
            row = await cur.fetchone()
            if row is not None and str(row["day"] or "") == str(day):
                return False
            await conn.execute(
                "INSERT INTO factory_alerts (line_key, day, alerted_at) VALUES (?, ?, ?)"
                " ON CONFLICT(line_key) DO UPDATE SET day = excluded.day,"
                " alerted_at = excluded.alerted_at",
                (str(line_key), str(day), time.time()),
            )
            return True

        return await self._write_txn(txn)

    # ---- 任务编排（pipelines / pipeline_nodes）：按依赖顺序自动跑的 Agent 任务 ----
    # 流水线是「草稿 → 运行 → 终态」的容器；节点是 DAG 上的一个 headless 运行，
    # 依赖满足（dep_mode: all/any）才就绪，由服务层编排循环调度执行。

    @staticmethod
    def _pipeline_row(r) -> dict:
        return {
            "id": r["id"],
            "project_id": r["project_id"],
            "name": r["name"],
            "status": r["status"],
            "concurrency": r["concurrency"],
            "notify_channel": bool(r["notify_channel"]),
            "created_at": r["created_at"],
            "finished_at": r["finished_at"],
        }

    @staticmethod
    def _pipeline_node_row(r) -> dict:
        keys = r.keys()
        return {
            "id": r["id"],
            "pipeline_id": r["pipeline_id"],
            "seq": r["seq"],
            "title": r["title"],
            "prompt": r["prompt"],
            "allowed_tools": [t for t in (r["allowed_tools"] or "").split(",") if t],
            "depends_on": [
                int(x) for x in (r["depends_on"] or "").split(",") if x.strip().lstrip("-").isdigit()
            ],
            "dep_mode": r["dep_mode"],
            "kind": r["kind"],
            "ref_id": r["ref_id"],
            "control": r["control"] if "control" in keys else "",
            "max_runs": r["max_runs"] if "max_runs" in keys else 1,
            "timeout_s": r["timeout_s"] if "timeout_s" in keys else 3600,
            "status": r["status"],
            "result": r["result"],
            "last_error": r["last_error"],
            "session_id": r["session_id"],
            "runs": r["runs"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
        }

    async def list_pipelines(
        self,
        project_id: int | None = None,
        *,
        status: str | None = None,
    ) -> list[dict]:
        """流水线列表（含全部节点），新建序倒序；批量拉取节点避免 N+1 查询。"""
        assert self._db
        clauses: list[str] = []
        args: list = []
        if project_id is not None:
            clauses.append("project_id = ?")
            args.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(str(status))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = await self._db.execute(
            f"SELECT * FROM pipelines{where} ORDER BY created_at DESC, id DESC",
            tuple(args),
        )
        prows = await cur.fetchall()
        if not prows:
            return []
        pipes = [self._pipeline_row(pr) for pr in prows]
        by_pid: dict[int, list[dict]] = {p["id"]: [] for p in pipes}
        placeholders = ",".join("?" for _ in by_pid)
        ncur = await self._db.execute(
            f"SELECT * FROM pipeline_nodes WHERE pipeline_id IN ({placeholders})"
            " ORDER BY pipeline_id ASC, seq ASC, id ASC",
            tuple(by_pid.keys()),
        )
        for nr in await ncur.fetchall():
            node = self._pipeline_node_row(nr)
            bucket = by_pid.get(node["pipeline_id"])
            if bucket is not None:
                bucket.append(node)
        for p in pipes:
            p["nodes"] = by_pid[p["id"]]
        return pipes

    async def get_pipeline(self, pipeline_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        pipe = self._pipeline_row(row)
        pipe["nodes"] = await self.list_pipeline_nodes(pipeline_id)
        return pipe

    async def get_pipeline_node(self, node_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM pipeline_nodes WHERE id = ?", (node_id,))
        row = await cur.fetchone()
        return self._pipeline_node_row(row) if row else None

    async def list_pipeline_nodes(self, pipeline_id: int) -> list[dict]:
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM pipeline_nodes WHERE pipeline_id = ? ORDER BY seq ASC, id ASC",
            (pipeline_id,),
        )
        rows = await cur.fetchall()
        return [self._pipeline_node_row(r) for r in rows]

    async def add_pipeline(
        self,
        project_id: int,
        name: str,
        nodes: list[dict] | None = None,
        concurrency: int = 2,
        notify_channel: bool = False,
    ) -> dict:
        """建流水线（草稿态）+ 一次性落全部节点。

        nodes 里每项的 depends_on 用「同批次序号」（0 起，见 tools/pipeline.py 的
        after 字段），这里统一物化成真实节点 id——对外（WS/工具查询）只有 id 一种引用。
        流水线行与全部节点行走独立写事务：任一节点插入失败整体回滚，
        不会留下没有节点的孤儿流水线。
        """
        assert self._db
        nodes = list(nodes or [])
        if len(nodes) > self.MAX_PIPELINE_NODES:
            raise ValueError(f"一条流水线最多 {self.MAX_PIPELINE_NODES} 个节点")
        if len(name) > self.MAX_PIPELINE_NAME_CHARS:
            raise ValueError(f"流水线名不能超过 {self.MAX_PIPELINE_NAME_CHARS} 个字")
        for n in nodes:
            self._validate_node_text(n.get("title") or "", n.get("prompt") or "")
        now = time.time()

        async def txn(conn: aiosqlite.Connection) -> int:
            cur = await conn.execute(
                "INSERT INTO pipelines (project_id, name, status, concurrency, notify_channel, created_at)"
                " VALUES (?, ?, 'draft', ?, ?, ?)",
                (
                    project_id,
                    name,
                    max(1, min(4, int(concurrency or 2))),
                    1 if notify_channel else 0,
                    now,
                ),
            )
            pid = cur.lastrowid
            ids: list[int] = []
            for i, n in enumerate(nodes or []):
                deps = await self._materialize_deps(n.get("depends_on") or [], ids)
                nid = await self._insert_node(
                    pid,
                    i,
                    n.get("title") or f"节点 {i + 1}",
                    n.get("prompt") or "",
                    n.get("allowed_tools") or [],
                    deps,
                    n.get("dep_mode") or "all",
                    kind=n.get("kind") or "run",
                    ref_id=str(n.get("ref_id") or ""),
                    control=str(n.get("control") or ""),
                    max_runs=int(n.get("max_runs") or 1),
                    timeout_s=n.get("timeout_s") if n.get("timeout_s") is not None else 3600,
                    conn=conn,
                )
                ids.append(nid)
            return pid

        pid = await self._write_txn(txn)
        return await self.get_pipeline(pid)

    async def add_pipeline_node(
        self,
        pipeline_id: int,
        title: str,
        prompt: str,
        allowed_tools: list[str] | None = None,
        depends_on: list[int] | None = None,
        dep_mode: str = "all",
        kind: str = "run",
        ref_id: str = "",
        timeout_s: int = 3600,
    ) -> dict:
        """追加节点；depends_on 直接给真实节点 id（必须是同流水线的节点）。

        kind：run=无人值守新会话（默认）/ task=挂接任务簿任务 / session=指定会话续跑；
        task / session 节点的 ref_id 分别是任务簿任务 id 与会话 id。
        """
        assert self._db
        if kind not in ("run", "task", "session"):
            raise ValueError(f"未知节点类型：{kind}")
        self._validate_node_text(title, prompt)
        existing = await self.list_pipeline_nodes(pipeline_id)
        if len(existing) >= self.MAX_PIPELINE_NODES:
            raise ValueError(f"一条流水线最多 {self.MAX_PIPELINE_NODES} 个节点")
        await self._check_dep_refs(pipeline_id, depends_on or [], exclude_id=None)
        seq = max((n["seq"] for n in existing), default=-1) + 1
        nid = await self._insert_node(
            pipeline_id,
            seq,
            title,
            prompt,
            allowed_tools or [],
            depends_on or [],
            dep_mode,
            kind=kind,
            ref_id=ref_id,
            timeout_s=timeout_s,
        )
        await self._db.commit()
        return await self.get_pipeline_node(nid)

    @classmethod
    def _validate_node_text(cls, title: str, prompt: str) -> None:
        """节点文本的长度上限（超限报中文错，而不是默默截断让指令失真）。"""
        if len(title) > cls.MAX_NODE_TITLE_CHARS:
            raise ValueError(f"节点名不能超过 {cls.MAX_NODE_TITLE_CHARS} 个字")
        if len(prompt) > cls.MAX_NODE_PROMPT_CHARS:
            raise ValueError(f"节点指令不能超过 {cls.MAX_NODE_PROMPT_CHARS} 个字")

    async def _insert_node(
        self,
        pipeline_id: int,
        seq: int,
        title: str,
        prompt: str,
        allowed_tools: list[str],
        depends_on: list[int],
        dep_mode: str,
        kind: str = "run",
        ref_id: str = "",
        control: str = "",
        max_runs: int = 1,
        timeout_s: int = 3600,
        conn: aiosqlite.Connection | None = None,
    ) -> int:
        """插入节点行返回 id；conn 传独立事务连接时语句落进该事务
        （add_pipeline 的全有全无建线），缺省仍写共享连接（单语句路径）。"""
        if conn is None:
            assert self._db
            conn = self._db
        cur = await conn.execute(
            "INSERT INTO pipeline_nodes (pipeline_id, seq, title, prompt, allowed_tools,"
            " depends_on, dep_mode, kind, ref_id, control, max_runs, timeout_s, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'blocked')",
            (
                pipeline_id,
                seq,
                title,
                prompt,
                ",".join(allowed_tools),
                ",".join(str(int(x)) for x in depends_on),
                dep_mode,
                kind,
                ref_id,
                control,
                max(1, int(max_runs or 1)),
                max(0, int(timeout_s if timeout_s is not None else 3600)),
            ),
        )
        return cur.lastrowid

    async def _materialize_deps(self, deps: list, ids: list[int]) -> list[int]:
        """把同批次序号物化成节点 id；越界序号直接丢弃（宽松处理，避免建流水线被卡死）。"""
        out = []
        for d in deps or []:
            try:
                idx = int(d)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(ids):
                out.append(ids[idx])
        return out

    async def _check_dep_refs(
        self,
        pipeline_id: int,
        depends_on: list[int],
        exclude_id: int | None,
    ) -> None:
        """依赖必须是同流水线的其他节点；并做环检测（改依赖时可能成环）。"""
        valid = {
            n["id"]
            for n in await self.list_pipeline_nodes(pipeline_id)
            if n["id"] != exclude_id
        }
        for d in depends_on or []:
            if int(d) not in valid:
                raise ValueError(f"依赖的节点 #{d} 不存在（或不是本流水线的节点）")

    async def has_dependency_cycle(self, pipeline_id: int, node_id: int, new_deps: list[int]) -> bool:
        """假设把 node_id 的依赖改成 new_deps，图里是否会出现环。

        节点很少（个位数），DFS 足够。node_id 引用自己也按环处理。
        """
        graph = {
            n["id"]: [int(d) for d in n["depends_on"]]
            for n in await self.list_pipeline_nodes(pipeline_id)
        }
        graph[node_id] = [int(d) for d in new_deps]
        if node_id in graph[node_id]:
            return True
        seen: set[int] = set()

        def walk(cur: int, path: set[int]) -> bool:
            if cur in path:
                return True
            if cur in seen:
                return False
            path.add(cur)
            for nxt in graph.get(cur, []):
                if nxt in graph and walk(nxt, path):
                    return True
            path.discard(cur)
            seen.add(cur)
            return False

        return any(walk(nid, set()) for nid in list(graph))

    async def update_pipeline_node(self, node_id: int, **kw) -> dict | None:
        assert self._db
        if "title" in kw or "prompt" in kw:
            node = await self.get_pipeline_node(node_id)
            if node is not None:
                self._validate_node_text(
                    kw.get("title", node["title"]), kw.get("prompt", node["prompt"])
                )
        allowed = (
            "seq",
            "title",
            "prompt",
            "allowed_tools",
            "depends_on",
            "dep_mode",
            "status",
            "result",
            "last_error",
            "session_id",
            "runs",
            "started_at",
            "finished_at",
            "control",
            "max_runs",
            "timeout_s",
        )
        fields, args = self._build_updates(
            kw,
            allowed,
            {
                "allowed_tools": lambda v: ",".join(str(x) for x in (v or [])),
                "depends_on": lambda v: ",".join(str(int(x)) for x in (v or [])),
                "timeout_s": lambda v: max(0, int(v or 0)),
            },
        )
        if not fields:
            return await self.get_pipeline_node(node_id)
        args.append(node_id)
        await self._db.execute(
            f"UPDATE pipeline_nodes SET {', '.join(fields)} WHERE id = ?", args
        )
        await self._db.commit()
        return await self.get_pipeline_node(node_id)

    async def update_pipeline(self, pipeline_id: int, **kw) -> dict | None:
        assert self._db
        # only_if_status：条件写入（CAS）。状态已被并发路径改走时不动库、
        # 返回 None——收尾判定与终止节点/用户停止抢收尾时靠它让位，不覆盖。
        only_if_status = kw.pop("only_if_status", None)
        allowed = ("name", "status", "concurrency", "finished_at", "notify_channel")
        fields, args = self._build_updates(
            kw,
            allowed,
            {
                "concurrency": lambda v: max(1, min(4, int(v or 2))),
                "notify_channel": lambda v: 1 if v else 0,
            },
        )
        if not fields:
            return await self.get_pipeline(pipeline_id)
        sql = f"UPDATE pipelines SET {', '.join(fields)} WHERE id = ?"
        args.append(pipeline_id)
        if only_if_status is not None:
            sql += " AND status = ?"
            args.append(only_if_status)
        cur = await self._db.execute(sql, args)
        if only_if_status is not None and cur.rowcount == 0:
            return None
        await self._db.commit()
        return await self.get_pipeline(pipeline_id)

    async def delete_pipeline_node(self, node_id: int) -> bool:
        assert self._db
        node = await self.get_pipeline_node(node_id)
        if node is None:
            return False
        updates: list[tuple[str, int]] = []
        for n in await self.list_pipeline_nodes(node["pipeline_id"]):
            if node_id in n["depends_on"]:
                new_deps = ",".join(
                    str(int(d)) for d in n["depends_on"] if d != node_id
                )
                updates.append((new_deps, n["id"]))

        async def txn(conn: aiosqlite.Connection) -> bool:
            if updates:
                await conn.executemany(
                    "UPDATE pipeline_nodes SET depends_on = ? WHERE id = ?",
                    updates,
                )
            cur = await conn.execute("DELETE FROM pipeline_nodes WHERE id = ?", (node_id,))
            return cur.rowcount > 0

        return await self._write_txn(txn)

    async def delete_pipeline(self, pipeline_id: int) -> bool:
        assert self._db

        async def txn(conn: aiosqlite.Connection) -> int:
            await conn.execute(
                "DELETE FROM pipeline_nodes WHERE pipeline_id = ?", (pipeline_id,)
            )
            cur = await conn.execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,))
            return cur.rowcount

        # 节点与流水线同一事务：要么全删、要么不删——中途失败整体回滚，不会
        # 留下「流水线没了但孤儿节点还在」的半删除，也不会被其他协程的
        # commit 把中间状态提前落盘（与 delete_project 同一口径）。
        return (await self._write_txn(txn)) > 0

    async def pipeline_usage(self, pipeline_id: int) -> dict:
        """流水线的 token 用量汇总：节点产出的会话都记在 sessions 表（title 带 ⚙
        前缀），按 session_id 聚合 usage_log。用量列是新增能力，旧数据自然为 0。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT COALESCE(SUM(l.in_tokens), 0) AS in_tokens,"
            " COALESCE(SUM(l.out_tokens), 0) AS out_tokens"
            " FROM usage_log l JOIN pipeline_nodes n ON n.session_id = l.session_id"
            " WHERE n.pipeline_id = ?",
            (pipeline_id,),
        )
        row = await cur.fetchone()
        return {"in_tokens": int(row["in_tokens"]), "out_tokens": int(row["out_tokens"])}

    async def reset_interrupted_pipelines(self) -> int:
        """应用启动时调用：上次运行中被中断的节点标为错误，就绪态退回等待。

        中断的节点不自动重跑（无人值守运行恢复执行有风险），由用户在
        「任务编排」面板手动重跑；流水线保持 running，其余节点照常调度。
        """
        assert self._db

        async def txn(conn: aiosqlite.Connection) -> int:
            await conn.execute(
                "UPDATE pipeline_nodes SET status = 'blocked' WHERE status = 'ready'"
            )
            cur = await conn.execute(
                "UPDATE pipeline_nodes SET status = 'error',"
                " last_error = '应用重启时运行被中断，可手动重跑', finished_at = ?"
                " WHERE status = 'running'",
                (time.time(),),
            )
            return cur.rowcount

        return await self._write_txn(txn)

    @staticmethod
    def compute_next_run(row: dict, now: float | None = None) -> float:
        """按调度类型计算下次运行时间（相对 last_run_at / 当前时间）。

        类型：interval / daily / weekly / workdays（周一至周五）/ cron
        （5 段表达式，存在 time_of_day 列里——纯文本列，无需加列迁移）/
        watch（文件夹变化触发：同样存在 time_of_day 列里，JSON 规格见
        core/watchspec.py；不按时间到点，返回 0 停放）。
        """
        n = now if now is not None else time.time()
        stype = row.get("schedule_type", "interval")
        if stype == "watch":
            # 文件夹变化触发（A2）：不按时间到点。0 = 停放（due_cron_tasks 只收
            # next_run_at > 0），由后端 watch 轮询器发现变化后把它顶到当前时刻，
            # 交给既有扫描循环执行；跑完这里重新归 0。
            return 0.0
        if stype == "interval":
            step = max(1, int(row.get("interval_minutes") or 0))
            base = row.get("last_run_at") or n
            return base + step * 60
        if stype == "cron":
            from ..core import cron_expr

            try:
                return cron_expr.next_run(str(row.get("time_of_day") or ""), n)
            except cron_expr.CronExprError:
                # 表达式坏了不能按 daily 静默跑（频次可能放大）；推到明天由
                # 触发时的错误呈现暴露问题，入口校验已在 cron.add/update 拦过一次
                return n + 24 * 3600
        try:
            hh, mm = map(int, str(row.get("time_of_day") or "00:00").split(":"))
        except ValueError:
            return n + 24 * 3600
        if stype == "daily":
            lt = time.localtime(n)
            today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
            return today if today > n else today + 24 * 3600
        if stype == "workdays":
            # 工作日每天：跳过周六（5）/周日（6）；今天的点已过就找下一个工作日
            lt = time.localtime(n)
            day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
            for _ in range(8):
                if day > n and time.localtime(day).tm_wday < 5:
                    return day
                day += 24 * 3600
            return n + 24 * 3600
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
