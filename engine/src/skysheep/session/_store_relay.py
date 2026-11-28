"""SessionStore 的接力模式（无工具串行 + 主副屏 pair/turn）持久化方法。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import aiosqlite

from ._store_types import _RELAY_UNSET, Session, _relay_json_load


class RelayStoreMixin:
    # ---- 接力（无工具串行 A/B，运行期） ----
    #
    # 独立短写事务：接力任务每次状态变更都要一次 CAS，且必须能在共享 aiosqlite
    # 事务之外独立提交——不能借用 self._db，共享连接上的多语句事务会被其他协程
    # 的语句穿插，破坏原子性。因此每条写路径开一条独立连接 BEGIN IMMEDIATE，
    # 参数化 SQL，短事务提交后立刻关闭。**绝不在该事务内 await provider / 窗口 /
    # 其他会自行 commit 的存储 API**（调用方保证）；事务内的读写全部是本地同步。

    RELAY_TERMINAL_STATES: tuple[str, ...] = ("stopped", "completed")

    @staticmethod
    def _relay_cas_ok(
        row, expect_states: tuple[str, ...] | None, expected_revision: int | None
    ) -> bool:
        if row is None:
            return False
        if expect_states is not None and row["state"] not in expect_states:
            return False
        if expected_revision is not None and int(row["revision"]) != int(expected_revision):
            return False
        return True

    @asynccontextmanager
    async def _relay_txn(self):
        """接力表专用写事务（形态保留，relay 各写路径照旧）；骨架由 _immediate_txn
        提供（独立连接 + BEGIN IMMEDIATE + 异常回滚，pragmas 与主连接同口径）。"""
        async with self._immediate_txn() as conn:
            yield conn

    @staticmethod
    def _relay_task_row(row) -> dict:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "state": row["state"],
            "current_node": row["current_node"],
            "active_slot": row["active_slot"],
            "generation": int(row["generation"]),
            "revision": int(row["revision"]),
            "instruction_revision": int(row["instruction_revision"]),
            "step": int(row["step"]),
            "owner_instance": row["owner_instance"],
            "goal": row["goal"],
            "config": _relay_json_load(row["config_json"], {}),
            "model_snapshot": _relay_json_load(row["model_snapshot_json"], {}),
            "error": _relay_json_load(row["error_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _relay_node_row(row) -> dict:
        return {
            "task_id": row["task_id"],
            "node_id": row["node_id"],
            "slot": row["slot"],
            "ordinal": int(row["ordinal"]),
            "status": row["status"],
            "terminal": bool(row["terminal"]),
            "prompt": row["prompt"],
            "report": row["report"],
            "report_hash": row["report_hash"],
            "report_id": row["report_id"],
            "attempt": int(row["attempt"]),
            "usage": _relay_json_load(row["usage_json"], {}),
            "error": _relay_json_load(row["error_json"], {}),
            "updated_at": row["updated_at"],
        }

    async def _relay_snapshot_conn(self, conn, task_id: str) -> dict | None:
        cur = await conn.execute("SELECT * FROM relay_tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        cur = await conn.execute(
            "SELECT * FROM relay_nodes WHERE task_id = ? ORDER BY ordinal", (task_id,)
        )
        nodes = [self._relay_node_row(r) for r in await cur.fetchall()]
        return {"task": self._relay_task_row(row), "nodes": nodes}

    @asynccontextmanager
    async def _relay_read_txn(self):
        """只读短事务：让多条 SELECT 落在同一读快照上（WAL 下不阻塞写）。

        为什么不能借共享连接：共享 self._db 上其他协程随时在跑语句，在这里
        BEGIN 会把别人的语句卷进本事务，且事务边界互相穿插。独立连接 +
        BEGIN DEFERRED 是本地文件上最省的做法——只读事务关闭即回滚。
        """
        conn = await aiosqlite.connect(self.path)
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("BEGIN DEFERRED")
            yield conn
        finally:
            await conn.close()

    async def relay_get(self, task_id: str) -> dict | None:
        """读任务快照：task 行与 nodes 行必须来自同一读快照。

        这份快照同时是调用方做 CAS 的 revision 基准（approve / pause /
        resume 都拿它提交）。分两次独立 SELECT 会在「worker 提交节点状态
        变更」的瞬间读到「旧 revision + 新 node」，调用方据此提交必然
        relay_conflict，表现为用户点暂停被告知「任务已变化」，机器越忙
        越容易命中。故走单读事务。
        """
        assert self._db
        async with self._relay_read_txn() as conn:
            return await self._relay_snapshot_conn(conn, task_id)

    async def relay_get_task(self, task_id: str) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM relay_tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return self._relay_task_row(row) if row else None

    async def relay_nodes(self, task_id: str) -> list[dict]:
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM relay_nodes WHERE task_id = ? ORDER BY ordinal", (task_id,)
        )
        return [self._relay_node_row(r) for r in await cur.fetchall()]

    async def relay_list_tasks(self, project_id: int | None, limit: int = 20) -> list[dict]:
        """项目内任务列表（新到旧）；project_id 为 None 表示快聊（无项目）任务。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM relay_tasks WHERE project_id IS ? ORDER BY created_at DESC LIMIT ?",
            (project_id, int(limit)),
        )
        return [self._relay_task_row(r) for r in await cur.fetchall()]

    async def relay_list_active_tasks(self, limit: int = 20) -> list[dict]:
        """全项目未终态接力任务（新到旧，供工厂跨项目观战）。"""
        assert self._db
        placeholders = ",".join("?" for _ in self.RELAY_TERMINAL_STATES)
        cur = await self._db.execute(
            f"SELECT * FROM relay_tasks WHERE state NOT IN ({placeholders})"
            " ORDER BY updated_at DESC LIMIT ?",
            (*self.RELAY_TERMINAL_STATES, max(1, min(100, int(limit)))),
        )
        return [self._relay_task_row(r) for r in await cur.fetchall()]

    async def session_project_map(self, session_ids: list[str]) -> dict[str, dict]:
        """批量查会话归属项目与标题（去重一次查询，供工厂实时跑线归属项目）。"""
        assert self._db
        ids = list(dict.fromkeys(str(s) for s in session_ids if str(s or "").strip()))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        cur = await self._db.execute(
            f"SELECT id, title, project_id FROM sessions WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return {
            str(r["id"]): {
                "title": str(r["title"] or ""),
                "project_id": int(r["project_id"]) if r["project_id"] is not None else None,
            }
            for r in await cur.fetchall()
        }

    async def relay_create(self, task: dict, nodes: list[dict]) -> dict:
        """新建接力任务与其全部节点（一条独立事务；创建成功不代表授予执行权）。"""
        now = time.time()
        async with self._relay_txn() as conn:
            await conn.execute(
                "INSERT INTO relay_tasks (id, project_id, session_id, state, current_node,"
                " active_slot, generation, revision, instruction_revision, step, owner_instance,"
                " goal, config_json, model_snapshot_json, error_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task["id"],
                    task["project_id"],
                    task.get("session_id", ""),
                    task["state"],
                    task["current_node"],
                    task.get("active_slot", ""),
                    int(task.get("generation", 0)),
                    int(task.get("revision", 0)),
                    int(task.get("instruction_revision", 0)),
                    int(task.get("step", 0)),
                    task.get("owner_instance", ""),
                    task.get("goal", ""),
                    json.dumps(task.get("config", {}), ensure_ascii=False),
                    json.dumps(task.get("model_snapshot", {}), ensure_ascii=False),
                    json.dumps(task.get("error", {}), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            for n in nodes:
                await conn.execute(
                    "INSERT INTO relay_nodes (task_id, node_id, slot, ordinal, status, terminal,"
                    " prompt, report, report_hash, report_id, attempt, usage_json, error_json,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task["id"],
                        n["node_id"],
                        n.get("slot", ""),
                        int(n.get("ordinal", 0)),
                        n.get("status", "pending"),
                        1 if n.get("terminal") else 0,
                        n.get("prompt", ""),
                        "",
                        "",
                        "",
                        0,
                        "{}",
                        "{}",
                        now,
                    ),
                )
        snap = await self.relay_get(task["id"])
        assert snap is not None
        return snap

    async def relay_mutate(
        self,
        task_id: str,
        *,
        request_id: str | None = None,
        action: str = "",
        expect_states: tuple[str, ...] | None = None,
        expected_revision: int | None = None,
        new_state: str | None = None,
        active_slot: str | None = None,
        current_node: object = _RELAY_UNSET,
        bump_generation: bool = False,
        step_delta: int = 0,
        error: object = _RELAY_UNSET,
        node_id: str | None = None,
        node_status: str | None = None,
        node_report: str | None = None,
        node_report_hash: str | None = None,
        node_report_id: str | None = None,
        node_attempt: int | None = None,
        node_usage: dict | None = None,
        node_error: object = _RELAY_UNSET,
        next_node_id: str | None = None,
        next_node_status: str | None = None,
    ) -> dict | None:
        """一次 CAS 状态变更（独立短事务）。

        返回变更后的快照 dict（含 "replayed" 标记）；CAS 不通过（状态或 revision
        不匹配，或任务不存在）返回 None。带 request_id 时先查 relay_actions：
        重复 request_id 直接返回已存结果，不重复变更。
        """
        now = time.time()
        async with self._relay_txn() as conn:
            if request_id:
                cur = await conn.execute(
                    "SELECT result_json FROM relay_actions WHERE task_id = ? AND request_id = ?",
                    (task_id, request_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    stored = _relay_json_load(row["result_json"], None)
                    if stored is not None:
                        return {**stored, "replayed": True}
            cur = await conn.execute("SELECT * FROM relay_tasks WHERE id = ?", (task_id,))
            trow = await cur.fetchone()
            if not self._relay_cas_ok(trow, expect_states, expected_revision):
                return None
            sets = ["revision = revision + 1", "updated_at = ?"]
            params: list = [now]
            if new_state is not None:
                sets.append("state = ?")
                params.append(new_state)
            if active_slot is not None:
                sets.append("active_slot = ?")
                params.append(active_slot)
            if current_node is not _RELAY_UNSET:
                sets.append("current_node = ?")
                params.append(str(current_node))
            if bump_generation:
                sets.append("generation = generation + 1")
            if step_delta:
                sets.append("step = step + ?")
                params.append(int(step_delta))
            if error is not _RELAY_UNSET:
                sets.append("error_json = ?")
                params.append(json.dumps(error or {}, ensure_ascii=False))
            params.append(task_id)
            await conn.execute(
                f"UPDATE relay_tasks SET {', '.join(sets)} WHERE id = ?", tuple(params)
            )
            node_changed = False
            if node_id is not None:
                nsets = ["updated_at = ?"]
                nparams: list = [now]
                for col, val in (
                    ("status", node_status),
                    ("report", node_report),
                    ("report_hash", node_report_hash),
                    ("report_id", node_report_id),
                ):
                    if val is not None:
                        nsets.append(f"{col} = ?")
                        nparams.append(val)
                if node_attempt is not None:
                    nsets.append("attempt = ?")
                    nparams.append(int(node_attempt))
                if node_usage is not None:
                    nsets.append("usage_json = ?")
                    nparams.append(json.dumps(node_usage, ensure_ascii=False))
                if node_error is not _RELAY_UNSET:
                    nsets.append("error_json = ?")
                    nparams.append(json.dumps(node_error or {}, ensure_ascii=False))
                nparams.extend([task_id, node_id])
                cur = await conn.execute(
                    f"UPDATE relay_nodes SET {', '.join(nsets)}"
                    " WHERE task_id = ? AND node_id = ?",
                    tuple(nparams),
                )
                node_changed = cur.rowcount == 1
            if next_node_id is not None and next_node_status is not None:
                await conn.execute(
                    "UPDATE relay_nodes SET status = ?, updated_at = ?"
                    " WHERE task_id = ? AND node_id = ?",
                    (next_node_status, now, task_id, next_node_id),
                )
            snap = await self._relay_snapshot_conn(conn, task_id)
            if snap is None:
                return None
            if node_changed and node_id is not None:
                snap["node"] = next(
                    (n for n in snap["nodes"] if n["node_id"] == node_id), None
                )
            if request_id:
                await conn.execute(
                    "INSERT OR REPLACE INTO relay_actions"
                    " (task_id, request_id, action, result_json, created_at) VALUES (?,?,?,?,?)",
                    (
                        task_id,
                        request_id,
                        action,
                        json.dumps(snap, ensure_ascii=False),
                        now,
                    ),
                )
        return snap

    async def relay_action_get(self, scope_id: str, request_id: str) -> dict | None:
        """幂等键查询（scope_id 可为任务 id 或 "start:<project_id>" 这类伪作用域）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT result_json FROM relay_actions WHERE task_id = ? AND request_id = ?",
            (scope_id, request_id),
        )
        row = await cur.fetchone()
        return _relay_json_load(row["result_json"], None) if row else None

    async def relay_action_put(
        self, scope_id: str, request_id: str, action: str, result: dict
    ) -> None:
        """登记一次幂等控制结果（独立短事务）。"""
        now = time.time()
        async with self._relay_txn() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO relay_actions"
                " (task_id, request_id, action, result_json, created_at) VALUES (?,?,?,?,?)",
                (scope_id, request_id, action, json.dumps(result, ensure_ascii=False), now),
            )

    async def relay_mark_interrupted(self, owner_instance: str) -> int:
        """启动恢复：把全部非终态任务标为 interrupted 并作废旧执行代际。

        只由持有数据目录独占锁的后端调用（否则第二个后端会把别的进程正在跑的
        活任务打断）。generation + 1 使所有旧 token 失效；不自动重放任何节点。
        """
        now = time.time()
        async with self._relay_txn() as conn:
            cur = await conn.execute(
                "SELECT id FROM relay_tasks WHERE state NOT IN (?, ?)",
                self.RELAY_TERMINAL_STATES,
            )
            ids = [r["id"] for r in await cur.fetchall()]
            for tid in ids:
                await conn.execute(
                    "UPDATE relay_tasks SET state = 'interrupted', generation = generation + 1,"
                    " revision = revision + 1, owner_instance = ?, error_json = ?, updated_at = ?"
                    " WHERE id = ?",
                    (
                        owner_instance,
                        json.dumps(
                            {
                                "code": "relay_interrupted",
                                "message": "应用重启，接力任务已中断；请核对后再显式恢复",
                            },
                            ensure_ascii=False,
                        ),
                        now,
                        tid,
                    ),
                )
            # 主副屏接力：运行中的 pair 在重启后同样没有执行者，标为 interrupted，
            # 不自动重放（重开会把一条指令再次发给模型，可能重复执行工具）
            await conn.execute(
                "UPDATE relay_pairs SET state = 'interrupted', revision = revision + 1,"
                " error_json = ?, updated_at = ? WHERE state = 'running'",
                (
                    json.dumps(
                        {
                            "code": "relay_interrupted",
                            "message": "应用重启，接力已中断；确认后再点执行继续",
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        return len(ids)

    # ---- 接力（主副屏交替）：pair / turn ----
    #
    # 与上面的「无工具串行 A/B」并存。pair 绑定两个正常会话：主屏是用户当前
    # 会话，副屏是独立的接力会话；每一轮通过 chat 管道跑完整 Agent（工具、
    # 权限门、检查点照常），这里只记录任务与轮次，绝不自己执行模型调用。

    @staticmethod
    def _relay_pair_row(row) -> dict:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "main_session_id": row["main_session_id"],
            "peer_session_id": row["peer_session_id"],
            "goal": row["goal"],
            "main_task": row["main_task"],
            "peer_task": row["peer_task"],
            "state": row["state"],
            "current_side": row["current_side"],
            "turn_no": int(row["turn_no"]),
            "auto_alternate": bool(row["auto_alternate"]),
            "verify_handoff": bool(row["verify_handoff"]),
            "revision": int(row["revision"]),
            "error": _relay_json_load(row["error_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _relay_turn_row(row) -> dict:
        return {
            "pair_id": row["pair_id"],
            "turn_no": int(row["turn_no"]),
            "side": row["side"],
            "status": row["status"],
            "brief": row["brief"],
            "output": row["output"],
            "error": row["error"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "verify_state": row["verify_state"],
            "verify_attempts": int(row["verify_attempts"]),
            "restatement": row["restatement"],
            "verify_note": row["verify_note"],
        }

    async def _relay_pair_snapshot_conn(self, conn, pair_id: str) -> dict | None:
        cur = await conn.execute("SELECT * FROM relay_pairs WHERE id = ?", (pair_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        cur = await conn.execute(
            "SELECT * FROM relay_turns WHERE pair_id = ? ORDER BY turn_no", (pair_id,)
        )
        return {
            "pair": self._relay_pair_row(row),
            "turns": [self._relay_turn_row(r) for r in await cur.fetchall()],
        }

    async def relay_pair_snapshot(self, pair_id: str) -> dict | None:
        assert self._db
        async with self._relay_read_txn() as conn:
            return await self._relay_pair_snapshot_conn(conn, pair_id)

    async def relay_pair_get(self, pair_id: str) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM relay_pairs WHERE id = ?", (pair_id,))
        row = await cur.fetchone()
        return self._relay_pair_row(row) if row else None

    async def relay_pair_latest(self, main_session_id: str) -> dict | None:
        """某个主屏会话最近一次的接力（新到旧；含已结束的，便于回看）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM relay_pairs WHERE main_session_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (main_session_id,),
        )
        row = await cur.fetchone()
        return self._relay_pair_row(row) if row else None

    async def relay_tasks_by_session(self, session_id: str) -> list[dict]:
        """串行接力任务按绑定会话查（新到旧；含终态，调用方自行筛）。

        删除会话/清项目的级联收敛用（接力模式设计 §7：删除入口必须先停止并
        收敛关联接力）——串行任务的绑定会话被删时任务要停到终态。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM relay_tasks WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        )
        return [self._relay_task_row(r) for r in await cur.fetchall()]

    async def relay_pairs_by_session(self, session_id: str) -> list[dict]:
        """主副屏 pair 按任一侧会话查（main 或 peer 命中都算；含终态，调用方
        自行筛）。删除会话的级联收敛要覆盖副屏会话被删的情形。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM relay_pairs WHERE main_session_id = ? OR peer_session_id = ?"
            " ORDER BY created_at DESC",
            (session_id, session_id),
        )
        return [self._relay_pair_row(r) for r in await cur.fetchall()]

    async def relay_pair_list(self, project_id: int | None, limit: int = 20) -> list[dict]:
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM relay_pairs WHERE project_id IS ? ORDER BY created_at DESC LIMIT ?",
            (project_id, int(limit)),
        )
        return [self._relay_pair_row(r) for r in await cur.fetchall()]

    async def relay_pair_create(self, pair: dict) -> dict:
        now = time.time()
        async with self._relay_txn() as conn:
            await conn.execute(
                "INSERT INTO relay_pairs (id, project_id, main_session_id, peer_session_id,"
                " goal, main_task, peer_task, state, current_side, turn_no, auto_alternate,"
                " verify_handoff, revision, error_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pair["id"],
                    pair.get("project_id"),
                    pair["main_session_id"],
                    pair["peer_session_id"],
                    pair.get("goal", ""),
                    pair.get("main_task", ""),
                    pair.get("peer_task", ""),
                    pair.get("state", "idle"),
                    pair.get("current_side", "main"),
                    int(pair.get("turn_no", 0)),
                    1 if pair.get("auto_alternate") else 0,
                    0 if pair.get("verify_handoff") is False else 1,
                    int(pair.get("revision", 0)),
                    json.dumps(pair.get("error") or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            snap = await self._relay_pair_snapshot_conn(conn, pair["id"])
        assert snap is not None
        return snap

    async def relay_pair_update(
        self,
        pair_id: str,
        fields: dict,
        *,
        expect_states: tuple[str, ...] | None = None,
        expected_revision: int | None = None,
        bump: bool = True,
    ) -> dict | None:
        """CAS 更新 pair（任务/状态/当前侧）；不匹配返回 None。

        bump=False 用于只改内容的保存（总任务/两屏任务/自动交替）：这类编辑
        不应该推进 revision——worker 开轮时按 revision 做 CAS，内容编辑把
        revision 顶掉会让 worker 的开轮失败、任务停在 running 无人推进。
        """
        allowed = {
            "goal",
            "main_task",
            "peer_task",
            "state",
            "current_side",
            "turn_no",
            "auto_alternate",
            "verify_handoff",
            "error",
            "peer_session_id",
        }
        now = time.time()
        async with self._relay_txn() as conn:
            cur = await conn.execute("SELECT * FROM relay_pairs WHERE id = ?", (pair_id,))
            row = await cur.fetchone()
            if not self._relay_cas_ok(row, expect_states, expected_revision):
                return None
            sets = ["updated_at = ?"]
            params: list = [now]
            if bump:
                sets.insert(0, "revision = revision + 1")
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "error":
                    sets.append("error_json = ?")
                    params.append(json.dumps(value or {}, ensure_ascii=False))
                elif key in ("auto_alternate", "verify_handoff"):
                    sets.append(f"{key} = ?")
                    params.append(1 if value else 0)
                elif key == "turn_no":
                    sets.append("turn_no = ?")
                    params.append(int(value))
                else:
                    sets.append(f"{key} = ?")
                    params.append(value)
            params.append(pair_id)
            await conn.execute(
                f"UPDATE relay_pairs SET {', '.join(sets)} WHERE id = ?", tuple(params)
            )
            snap = await self._relay_pair_snapshot_conn(conn, pair_id)
        return snap

    async def relay_pair_start_turn(
        self,
        pair_id: str,
        side: str,
        brief: str,
        expected_revision: int,
        verify: dict | None = None,
    ) -> dict | None:
        """开一轮：turn_no+1、写 running 的 turn、pair 置 running（同一事务）。

        verify 是交接门的留痕（verify_state/verify_attempts/restatement/
        verify_note），与本轮开轮在同一事务里落库；不传即「未经复述核验」
        （人工批准的第一轮或关闭核验的 pair）。
        并发保护：只有 state=running 且 revision 与调用方读到的一致才开轮，
        两个「执行」点击只有一个能落库。
        """
        now = time.time()
        verify = verify or {}
        async with self._relay_txn() as conn:
            cur = await conn.execute("SELECT * FROM relay_pairs WHERE id = ?", (pair_id,))
            row = await cur.fetchone()
            if not self._relay_cas_ok(row, ("running",), expected_revision):
                return None
            turn_no = int(row["turn_no"]) + 1
            await conn.execute(
                "INSERT INTO relay_turns (pair_id, turn_no, side, status, brief, output,"
                " error, started_at, finished_at, verify_state, verify_attempts,"
                " restatement, verify_note) VALUES (?,?,?,?,?,'','',?,0,?,?,?,?)",
                (
                    pair_id,
                    turn_no,
                    side,
                    "running",
                    brief,
                    now,
                    str(verify.get("state") or ""),
                    int(verify.get("attempts") or 0),
                    str(verify.get("restatement") or ""),
                    str(verify.get("note") or ""),
                ),
            )
            await conn.execute(
                "UPDATE relay_pairs SET turn_no = ?, current_side = ?, revision = revision + 1,"
                " updated_at = ? WHERE id = ?",
                (turn_no, side, now, pair_id),
            )
            snap = await self._relay_pair_snapshot_conn(conn, pair_id)
        return snap

    async def relay_pair_finish_turn(
        self,
        pair_id: str,
        turn_no: int,
        *,
        status: str,
        output: str = "",
        error: str = "",
        next_side: str | None = None,
        auto: bool = False,
        expect_turn_status: tuple[str, ...] | None = ("running",),
    ) -> dict | None:
        """收一轮：写 turn 结果并推进 pair 状态。

        done + auto → 保持 running（worker 接着跑对方）；
        done → idle（等用户点下一次执行）；cancelled → idle；其余 → paused。

        expect_turn_status：turn 行的 CAS 守卫（对齐 relay_pair_update /
        relay_pair_start_turn 的既有形态），默认只收「正在跑」的轮——守卫
        不过（轮已终态，如收尾广播上的取消落在 await 上、迟到的 abort）
        返回 None 且不动 pair：已成功的 done 轮不得被改写成 cancelled。
        turn 行不存在（开轮前的空任务收尾，turn_no=0）时守卫跳过，只落
        pair 状态。传 None 关闭守卫（预留，现无调用方）。
        """
        now = time.time()
        async with self._relay_txn() as conn:
            cur = await conn.execute("SELECT * FROM relay_pairs WHERE id = ?", (pair_id,))
            row = await cur.fetchone()
            if row is None:
                return None
            if expect_turn_status is not None:
                tcur = await conn.execute(
                    "SELECT status FROM relay_turns WHERE pair_id = ? AND turn_no = ?",
                    (pair_id, turn_no),
                )
                trow = await tcur.fetchone()
                if trow is not None and trow["status"] not in expect_turn_status:
                    return None
            await conn.execute(
                "UPDATE relay_turns SET status = ?, output = ?, error = ?, finished_at = ?"
                " WHERE pair_id = ? AND turn_no = ?",
                (status, output or "", error or "", now, pair_id, turn_no),
            )
            if status == "done":
                state = "running" if auto else "idle"
                err: dict = {}
            elif status == "cancelled":
                state = "idle"
                err = {"code": "relay_turn_cancelled", "message": "本轮已被中断"}
            else:
                state = "paused"
                err = {"code": "relay_turn_error", "message": (error or "执行失败")[:500]}
            sets = ["state = ?", "revision = revision + 1", "updated_at = ?", "error_json = ?"]
            params: list = [state, now, json.dumps(err, ensure_ascii=False)]
            if next_side:
                sets.append("current_side = ?")
                params.append(next_side)
            params.append(pair_id)
            await conn.execute(
                f"UPDATE relay_pairs SET {', '.join(sets)} WHERE id = ?", tuple(params)
            )
            snap = await self._relay_pair_snapshot_conn(conn, pair_id)
        return snap

    async def relay_peer_session(
        self,
        project_id: int | None,
        title: str,
        *,
        create: bool = True,
        exclude_pair_id: str | None = None,
    ) -> Session | None:
        """副屏会话：优先复用同项目里同名且还没有消息的空会话，否则新建。

        点「接力」会先开副屏、后填任务；不复用的话每点一次就多一条空会话。
        复用必须排他（对抗审查）：已被 relay_pairs 引用为副屏的空会话不再
        复用（被占即新建）——否则同项目两份接力会绑到同一条副屏，轮次交错
        互相当上下文。exclude_pair_id 是调用方自己的 pair：重绑自己的副屏
        不受影响。pair 行不删（历史保留只读），所以占用随 pair 存续、语义
        就是「一条副屏只属于一份接力」。
        进程内串行化：侧栏点击与右侧面板加载可能同时各发一次 dual_open，
        并发查询会各自查空、各建一条同名空会话，前端就开了两块副屏。
        create=False 用于「面板自动加载」：只找现成的空副屏，没有就返回 None，
        不能因为打开应用时右侧停在接力标签就凭空建一条会话。
        """
        lock = getattr(self, "_relay_peer_lock", None)
        if lock is None:
            lock = self._relay_peer_lock = asyncio.Lock()
        async with lock:
            assert self._db
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id IS ? AND title = ? AND archived = 0"
                " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id)"
                " AND id NOT IN (SELECT peer_session_id FROM relay_pairs"
                "                WHERE peer_session_id IS NOT NULL"
                "                AND (? IS NULL OR id != ?))"
                " ORDER BY updated_at DESC LIMIT 1",
                (project_id, title, exclude_pair_id, exclude_pair_id),
            )
            row = await cur.fetchone()
            if row is not None:
                return Session(
                    row["id"],
                    row["project_id"],
                    row["title"],
                    row["created_at"],
                    row["updated_at"],
                )
            if not create:
                return None
            return await self.create_session(project_id, title=title)

    async def relay_rename_orphan_peers(
        self,
        project_id: int | None,
        old_title: str,
        new_title: str,
    ) -> list[str]:
        """主屏改名时，把「还没绑定 pair 的孤儿副屏」按旧名推导一并对齐。

        副屏会话在点「接力」时就创建，relay_pairs 行要到第一次 run 才落库；
        这个窗口里主屏改名，副屏会 stranded 在旧名上——下次 relay_peer_session
        按新名找不到它、又建一条新的，多出一条空会话。只改「空 + 未被任何
        pair 占用」的旧名同名会话（与复用同一排他口径）；已绑定的副屏由
        pair 路径处理，有消息的副屏（用户可能已在里面聊过）不追改。
        旧名必须是上次会为这个主屏生成的副屏名（_relay_peer_title_for(旧主屏名)），
        这样连续多次改名也能一节节追上；旧名等于新名时无事可做直接返回。"""
        assert self._db
        if not old_title or old_title == new_title:
            return []
        async with self._relay_peer_lock:
            cur = await self._db.execute(
                "SELECT id FROM sessions WHERE project_id IS ? AND title = ?"
                " AND archived = 0"
                " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id)"
                " AND id NOT IN (SELECT peer_session_id FROM relay_pairs"
                "                WHERE peer_session_id IS NOT NULL)",
                (project_id, old_title),
            )
            ids = [r["id"] for r in await cur.fetchall()]
            for sid in ids:
                await self.set_title(sid, new_title)
            return ids

    async def relay_pair_latest_turn(self, pair_id: str) -> dict | None:
        """pair 最新一轮（轮间隙/协商窗口的新鲜度判定用，见 backend 侧守卫）。"""
        cur = await self._db.execute(
            "SELECT * FROM relay_turns WHERE pair_id = ? ORDER BY turn_no DESC LIMIT 1",
            (pair_id,),
        )
        row = await cur.fetchone()
        return self._relay_turn_row(row) if row else None

    async def relay_pair_last_done_output(
        self,
        pair_id: str,
        *,
        exclude_side: str,
    ) -> str:
        """对方最近一次非空成功输出（避免整本 relay_pair_snapshot 扫描）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT output FROM relay_turns"
            " WHERE pair_id = ? AND side != ? AND status = 'done' AND output != ''"
            " ORDER BY turn_no DESC",
            (pair_id, exclude_side),
        )
        async for row in cur:
            text = str(row["output"] or "").strip()
            if text:
                return text
        return ""
