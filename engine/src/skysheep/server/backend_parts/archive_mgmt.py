"""归档管理（设置 · 归档管理）：自动归档巡检、归档列表与批量恢复/删除。

与侧栏「归档会话」弹窗分工：弹窗只看当前项目（含快聊）、做单发恢复；
这里跨全部项目管理——自动归档开关与保留天数（config.toml 的 [archive]）、
按项目分组的归档列表、按批/全部删除与恢复。

自动归档巡检与保留策略巡检同一姿态（backend_parts/data_retention.py）：
启动延迟一轮、之后每小时看一眼；开关关闭时零开销跳过。只动 archived
标记不删数据；**不做记忆提炼**——手动归档的提炼是「用户宣告这事完了」
的显式时机，自动打扫是静默家务，不该顺手花用户的模型调用。
"""

from __future__ import annotations

import asyncio
import time

from ...config import ConfigError, load_config, set_archive_in_config
from ...obs import info as obs_info


class ArchiveMgmtMixin:
    """设置 · 归档管理：配置读写、跨项目归档列表、批量操作与自动归档循环。"""

    # 与保留策略巡检错峰：启动高峰过去再首扫（保留策略 45s，这里 2 分钟）；
    # 之后每小时看一眼——自动归档不急，晚一小时归档没有任何代价
    ARCHIVE_FIRST_DELAY_S = 120.0
    ARCHIVE_CHECK_INTERVAL_S = 3600.0
    # 单轮至多归档多少个：防「关了很久再开」或「刚把天数调小」时一口气
    # 归档上千个会话，广播与侧栏刷新被瞬间打满；剩下的下一轮继续
    ARCHIVE_PASS_LIMIT = 200

    def start_archive_loop(self) -> None:
        self._archive_task = asyncio.create_task(self._archive_loop())

    def stop_archive_loop(self) -> None:
        if getattr(self, "_archive_task", None):
            self._archive_task.cancel()
            self._archive_task = None

    async def _archive_loop(self) -> None:
        """延迟首扫 + 逐时巡检：单轮失败不终止循环（与保留策略同一姿态）。"""
        await asyncio.sleep(self.ARCHIVE_FIRST_DELAY_S)
        while True:
            try:
                await self._auto_archive_pass()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.ARCHIVE_CHECK_INTERVAL_S)

    def _archive_cfg(self):
        sec = getattr(self.cfg, "archive", None)
        return sec

    def _auto_archive_excluded_ids(self) -> set[str]:
        """运行期排除名单：正在跑/排队的会话与当前打开的会话不进候选。

        定时任务与流水线每次运行都新建会话行（automation.py），没有跨轮
        存活的会话 id，不在排除面里——它们的行在运行中会被 updated_at
        刷新，天然不是「最后活动超期」的候选。
        """
        excluded: set[str] = set()
        if self.session is not None and self.session.id:
            excluded.add(self.session.id)
        for sid, rt in self.runtimes.items():
            if (rt.run_task is not None and not rt.run_task.done()) or rt.queue:
                excluded.add(sid)
        return excluded

    async def _auto_archive_pass(self, *, now: float | None = None) -> list[str]:
        """自动归档一轮：返回本轮归档的会话 id（开关关闭返回空）。"""
        sec = self._archive_cfg()
        if sec is None or not sec.auto_archive:
            return []
        days = max(1, min(3650, int(sec.auto_after_days or 14)))
        cutoff = (time.time() if now is None else float(now)) - days * 86400.0
        candidates = await self.store.stale_session_ids(
            cutoff, self._auto_archive_excluded_ids(), limit=self.ARCHIVE_PASS_LIMIT
        )
        archived: list[str] = []
        for sid in candidates:
            sess = await self.store.get_session(sid)
            if sess is None or sess.archived or sess.pinned:
                continue  # 扫描与执行之间可能被置顶/手动归档：以当下状态为准
            await self.store.set_archived(sid, True)
            archived.append(sid)
            # 与手动归档同一事件形状：另一窗口的侧栏/标签同步收起该会话
            self._ws_broadcast({
                "kind": "session_updated", "session_id": sid,
                "title": sess.title, "archived": True,
            })
        if archived:
            obs_info(
                "auto_archive", f"自动归档 {len(archived)} 个会话",
                count=len(archived), days=days,
            )
        return archived

    # ---- 设置读写 ----

    async def archive_status(self) -> dict:
        """归档管理回读（设置页渲染用）：开关、保留天数与归档总数。"""
        sec = self._archive_cfg()
        count = await self.store.count_archived_sessions() if self.store is not None else 0
        return {
            "auto_archive": bool(sec is not None and sec.auto_archive),
            "auto_after_days": int(sec.auto_after_days) if sec is not None else 14,
            "archived_count": count,
        }

    async def archive_save(self, params: dict) -> dict:
        """保存自动归档配置（config.toml 的 [archive]）；即改即存、热生效。"""
        raw_days = params.get("auto_after_days")
        days: int | None = None
        if raw_days is not None and raw_days != "":
            if isinstance(raw_days, bool):
                raise RuntimeError("自动归档的保留时长需要一个整数")
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                raise RuntimeError("自动归档的保留时长需要一个整数") from None
        auto = params.get("auto_archive")
        try:
            set_archive_in_config(
                auto_archive=None if auto is None else bool(auto),
                auto_after_days=days,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()  # 热生效：下一轮巡检按新配置扫描
        return await self.archive_status()

    # ---- 跨项目管理列表与批量操作（本机界面专用，WS 层 local_only） ----

    async def archive_manage_list(self) -> dict:
        """全部项目的已归档会话（归档管理页）：按项目分组交给前端，最近活跃在前。

        归档弹窗按当前项目过滤（渠道/远程边界）；这里是本机管理视图，跨项目
        列出，项目名随行带上，前端不再单独查项目表。
        """
        sessions = await self.store.list_archived_sessions(limit=500)
        projects = await self.store.list_projects()
        return {
            "sessions": [
                {
                    "id": s.id, "title": s.title, "created_at": s.created_at,
                    "updated_at": s.updated_at, "project_id": s.project_id,
                    "pinned": bool(s.pinned), "summary": s.summary,
                    "tags": [t for t in str(s.tags or "").split(",") if t],
                }
                for s in sessions
            ],
            "projects": [{"id": p.id, "name": p.name} for p in projects],
        }

    async def archive_restore_batch(self, ids: list[str]) -> dict:
        """批量取消归档。只动库内标记；目标不存在/未归档的跳过不报错。"""
        restored: list[str] = []
        for sid in ids:
            sess = await self.store.get_session(sid)
            if sess is None or not sess.archived:
                continue
            await self.store.set_archived(sid, False)
            restored.append(sid)
            self._ws_broadcast({
                "kind": "session_updated", "session_id": sid,
                "title": sess.title, "archived": False,
            })
        return {"restored": len(restored), "ids": restored}

    async def archive_delete_batch(self, ids: list[str] | None = None) -> dict:
        """批量删除已归档会话；ids=None 表示全部已归档（「全部删除」）。

        只删已归档的：误传普通会话 id 一律跳过——删除是整链清理（停轮、
        检查点、广播），代价不该被一个手滑的 id 触发。逐个复用
        delete_session 的完整清理链；单个失败不中断整批。
        """
        if ids is None:
            targets = [s.id for s in await self.store.list_archived_sessions(limit=500)]
        else:
            targets = ids
        deleted: list[str] = []
        failed = 0
        for sid in targets:
            sess = await self.store.get_session(sid)
            if sess is None or not sess.archived:
                continue
            try:
                await self.delete_session(sid, local=True)
                deleted.append(sid)
            except Exception:  # noqa: BLE001 - 单个失败不拦整批，计入回报
                failed += 1
        return {"deleted": len(deleted), "ids": deleted, "failed": failed}
