"""记忆体系：归档自动提炼、定期整理、全局 memory.md 编辑与项目记忆地图。

从 backend.py 按职责注释整段搬入（提炼/整理纯函数在 tools/memory.py，
这里是与模型和存储接线的那一半）。方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from ...bgtasks import spawn_bg
from ...config import (
    ConfigError,
    load_config,
    set_advanced_settings_in_config,
    set_memory_maintenance_in_config,
    set_memory_map_config,
)
from ...core.checkpoints import CheckpointStore
from ...core.prompt import MAX_INSTRUCTIONS_CHARS
from ...messages import Message
from ...models.base import ProviderDone, ProviderTextDelta
from ...session.store import Project
from ...textio import write_text_atomic
from ...tools.memory import (
    MAINTAIN_MIN_GLOBAL_CHARS,
    MAINTAIN_MIN_PROJECT_CHARS,
    MAINTENANCE_BACKUP_KEEP,
    MAX_MEMORY_FILE_CHARS,
    backup_before_maintain,
    build_digest_prompt,
    build_maintain_prompt,
    clean_maintained_text,
    digest_transcript,
    load_maintenance_state,
    maintenance_due,
    memory_path,
    parse_digest,
    parse_memory_entries,
    remember_lines,
    save_maintenance_state,
)

logger = logging.getLogger("skysheep.security")
memory_log = logging.getLogger("skysheep.memory")

class MemoryMixin:
    """归档自动记忆、定期整理、全局记忆编辑与记忆地图。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- 归档自动记忆（提炼纯函数在 tools/memory.py） ----

    def _schedule_memory_digest(self, session_id: str) -> None:
        if not self.cfg.memory_digest:
            return  # 设置 · 全局记忆里关掉了归档自动记忆（保存即热生效）
        if session_id in self._digesting:
            return
        self._digesting.add(session_id)
        try:
            spawn_bg(self._memory_digest(session_id))
        except RuntimeError:
            self._digesting.discard(session_id)  # 无事件循环（如纯测试环境）

    async def _memory_digest(self, sid: str) -> None:
        """归档后用当前模型通读会话，提炼可长期记住的信息写入用户记忆。

        与 _auto_title 同一套边界：走当前 provider 但失败静默，绝不影响主流程；
        写入成功后像 save_instructions 一样全局刷新系统提示词，让所有会话
        下一轮就带上新记忆。
        """
        try:
            if getattr(self.provider, "demo_mode", False):
                return  # 演示模式不消耗脚本组
            transcript = digest_transcript(await self.store.load_messages(sid))
            if not transcript:
                return  # 寒暄/单条短问答，不值得提炼
            parts: list[str] = []
            async for ev in self.provider.stream(
                [Message.user(build_digest_prompt(transcript))], []
            ):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            added = await self._remember_digest_entries(parts)
            # 提炼跑完（无论有没有提出新条目）就标记：模型调用已经花过钱，
            # 取消归档再归档不应再来一遍；上面任何一步抛错则不标记，下次可重试
            await self.store.mark_session_memory_digested(sid)
            if not added:
                return
            for ag in self._for_each_agent():
                ag.set_system(self.compose_system())
            self._ws_broadcast({
                "kind": "memory_digest", "session_id": sid, "added": len(added),
                "message": f"归档后新记住 {len(added)} 条：" + "；".join(added)[:200],
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            memory_log.warning("archive memory digest failed: %s", e)
        finally:
            self._digesting.discard(sid)

    async def _remember_digest_entries(self, parts: list[str]) -> list[str]:
        """提炼稿解析落盘：走记忆写锁，避免与定期整理/设置页保存交错写 memory.md。"""
        async with self._memory_io_lock:
            return remember_lines(parse_digest("".join(parts)))

    # ---- 定期自动整理：按周期用模型合并去重全局/项目记忆（纯函数在 tools/memory.py） ----

    MEMORY_MAINTENANCE_INTERVAL = 600  # 巡检间隔秒数：到期后最多再等 10 分钟

    def start_memory_maintenance_loop(self) -> None:
        self._memory_maintenance_task = asyncio.create_task(self._memory_maintenance_loop())

    def stop_memory_maintenance_loop(self) -> None:
        if getattr(self, "_memory_maintenance_task", None):
            self._memory_maintenance_task.cancel()
            self._memory_maintenance_task = None

    async def _memory_maintenance_loop(self) -> None:
        # 先睡再巡检：启动时刻不做整理（刚打开应用不该立刻起模型调用，
        # 也避免与首归档提炼/手动整理抢同一个 provider 脚本）
        while True:
            await asyncio.sleep(self.MEMORY_MAINTENANCE_INTERVAL)
            try:
                await self._memory_maintenance_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮巡检失败不终止循环（与 cron/提醒循环同一姿态）

    async def _memory_maintenance_tick(self) -> None:
        st = self.cfg.memory_maintenance
        if self.provider is None or getattr(self.provider, "demo_mode", False):
            return
        due_g, due_p = maintenance_due(
            load_maintenance_state(),
            global_enabled=st.global_enabled, project_enabled=st.project_enabled,
            interval_hours=st.interval_hours, workdir=str(self.working_dir or ""),
            now=time.time(),
        )
        if due_g:
            await self._maintain_memory("global")
        if due_p:
            await self._maintain_memory("project")
        await self._map_auto_digest_tick()

    async def memory_maintain_now(self) -> dict:
        """手动「立即整理」：无视周期与开关（明确点击即用户意图），仍守阈值与规模。"""
        if self.provider is None:
            raise RuntimeError("还没有可用的模型服务，先在 设置 · 模型服务 配置 API Key")
        if getattr(self.provider, "demo_mode", False):
            # 与归档提炼/定时巡检同一守卫：演示模式只有脚本文本，真整理会拿脚本
            # 内容去覆盖 memory.md / AGENTS.md
            raise RuntimeError("演示模式没有真实模型，整理不了记忆；先在 设置 · 模型服务 配置")
        g = await self._maintain_memory("global", force=True)
        p = await self._maintain_memory("project", force=True)
        if "conflict" in (g, p):
            return {"ran": False, "message":
                    "记忆刚被归档提炼写入过新条目，本次整理已让路以免覆盖；稍后再点一次即可"}
        if "changed" in (g, p):
            return {"ran": True, "global": g == "changed", "project": p == "changed"}
        if g == "skipped" and p == "skipped":
            return {"ran": False, "message": "两份记忆都还没到需要整理的规模（全局 ≥400 字、项目 ≥600 字）"}
        if g == "unchanged" and p == "unchanged":
            return {"ran": False, "message": "整理完成：没有发现需要合并或删除的条目，记忆维持原样"}
        return {"ran": False, "message": "整理完成：跑过的部分无需改动（另一份还没到整理规模）"}

    async def _maintain_memory(self, scope: str, force: bool = False) -> str:
        """整理一份记忆文件：模型重写 → 备份原件 → 落盘 → 刷新系统提示词。

        返回 "changed"（整理并落盘）/ "unchanged"（模型认为无需改动或输出无效）
        / "skipped"（没到整理规模或上一轮还在跑）/ "conflict"（读旧稿后文件被
        并发写过，让路不写，不推进「上次整理时间」，下一轮巡检自动重试）。
        失败静默记日志（整理是锦上添花，绝不打扰主流程）；「立即整理」按四态
        分别给用户反馈。
        """
        if self._maintaining:
            return "skipped"  # 上一轮还没跑完（手动+定时叠加时直接让路）
        if scope == "global":
            path = memory_path()
            min_chars = MAINTAIN_MIN_GLOBAL_CHARS
            max_chars = MAX_MEMORY_FILE_CHARS
        else:
            path = Path(self.instructions_file) if self.instructions_file else None
            min_chars = MAINTAIN_MIN_PROJECT_CHARS
            max_chars = MAX_INSTRUCTIONS_CHARS
        old_text = ""
        if path is not None:
            try:
                old_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old_text = ""
        if len(old_text.strip()) < min_chars:
            return "skipped"  # 还没到值得整理的规模，也不消耗「上次整理时间」
        self._maintaining = True
        try:
            parts: list[str] = []
            async for ev in self.provider.stream(
                [Message.user(build_maintain_prompt(scope, old_text))], []
            ):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            new_text = clean_maintained_text("".join(parts), old_text, max_chars)
            if new_text is None:
                return "unchanged"  # 输出为空/与原文一致/超长失控/结构破坏：一律不动原文件
            async with self._memory_io_lock:
                # 读旧稿之后文件可能已被并发写过（归档提炼追加了新条目、设置页
                # 保存）：整理稿按旧快照生成，直接覆盖会把新内容静默抹掉。对不上
                # 就让路——整理时间不推进，巡检下一轮会带着新内容重新整理。
                try:
                    cur = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    cur = ""
                if cur != old_text:
                    return "conflict"
                try:
                    # 先备份后写：备份失败（磁盘满/权限）就放弃本次整理，不裸写
                    backup_before_maintain(path, old_text)
                    # 原子写（textio 同族）：写一半被杀不能留下半截记忆文件
                    write_text_atomic(path, new_text + "\n")
                except OSError as e:
                    memory_log.warning("memory maintain write failed (%s): %s", scope, e)
                    return "skipped"
            if scope == "project":
                self.instructions_text = new_text
            for ag in self._for_each_agent():
                ag.set_system(self.compose_system())
            state = load_maintenance_state()
            if scope == "global":
                state["global_last"] = time.time()
                msg = f"已整理全局记忆（原件已备份，同目录保留最近 {MAINTENANCE_BACKUP_KEEP} 份）"
            else:
                if self.working_dir is not None:
                    proj = dict(state.get("project_last") or {})
                    proj[str(self.working_dir)] = time.time()
                    state["project_last"] = proj
                msg = f"已整理项目记忆（AGENTS.md，原件已备份，同目录保留最近 {MAINTENANCE_BACKUP_KEEP} 份）"
            save_maintenance_state(state)
            if not force:  # 手动触发的结果在按钮状态行里看，不弹通知
                self._ws_broadcast({"kind": "memory_maintain", "scope": scope, "message": msg})
            return "changed"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            memory_log.warning("memory maintain failed (%s): %s", scope, e)
            return "skipped"
        finally:
            self._maintaining = False

    async def list_archived_sessions(self) -> dict:
        """归档弹窗列表（当前项目 + 快聊，最近活跃在前）。

        带上快聊：侧栏的快聊分组常驻，它的会话归档后如果不在这个弹窗里，
        用户就再也没地方恢复或删除它（归档弹窗是唯一入口）。
        """
        sessions = await self.store.list_archived_sessions(
            self._cur_project_id(), include_projectless=True
        )
        return {
            "sessions": [
                {
                    "id": s.id, "title": s.title, "updated_at": s.updated_at,
                    "pinned": bool(s.pinned), "project_id": s.project_id,
                    "summary": s.summary,
                    "tags": [t for t in str(s.tags or "").split(",") if t],
                    "archived": bool(s.archived),
                }
                for s in sessions
            ]
        }

    async def set_session_tags(self, session_id: str, tags: list[str] | str) -> dict:
        """给会话打标签（侧栏分组用）；传空清空。"""
        sess = await self._get_owned_session(session_id)
        value = await self.store.set_tags(session_id, tags)
        # 广播给其它连接：另一窗口的侧栏同步标签分组
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title})
        return {
            "id": session_id,
            "tags": [t for t in value.split(",") if t],
            "all_tags": await self.store.list_all_tags(self._cur_project_id()),
        }

    async def move_session(self, session_id: str, project_id: int | None) -> dict:
        """把会话移动到另一个项目；project_id=None 移入快聊。

        源会话必须属于当前项目（安全审查 B7）：旧实现只验目标项目存在，
        凭枚举到的 session_id 可以把别的项目的会话改归属。
        """
        sess = await self._get_owned_session(session_id)
        if project_id is not None:
            projects = {p.id: p for p in await self.store.list_projects()}
            if project_id not in projects:
                raise RuntimeError(f"project not found: {project_id}")
        await self.store.move_session(session_id, project_id)
        # 移出当前项目时同步丢弃 runtime：它带着旧项目的门控/工作目录，
        # 留着会让后续轮次错在别的项目上下文里执行（与 delete_session 对齐）；
        # 无项目态当前归属是快聊（None），移进任何项目都算移出
        if project_id != self._cur_project_id():
            self.cancel_run(session_id)
            rt = self.runtimes.pop(session_id, None)
            if rt is not None:
                # 排队轮随 runtime 消失：Future 逐个落空，请求不能挂死
                for item in list(rt.queue):
                    item.fail(RuntimeError("会话已移出当前项目"))
                rt.queue.clear()
                self._forget_runtime(rt)
        was_active = self.session and self.session.id == session_id
        moved_to_active = was_active and project_id == self._cur_project_id()
        switched = await self._switch_after_removal() if (was_active and not moved_to_active) else None
        # 广播给其它连接：另一窗口的侧栏按新归属刷新（本项目列表不再有它）
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title})
        return {"id": session_id, "project_id": project_id, "switched_to": switched}


    # ---- 全局记忆：设置页直接查看/编辑 memory.md ----

    async def memory_get(self) -> dict:
        from ...tools.memory import MAX_MEMORY_FILE_CHARS, inject_text, memory_path

        p = memory_path()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        try:
            mtime = round(p.stat().st_mtime, 3)
        except OSError:
            mtime = 0.0
        st = self.cfg.memory_maintenance
        state = load_maintenance_state()
        return {
            "path": str(p),
            "text": text[:MAX_MEMORY_FILE_CHARS],
            # mtime 供设置页保存时比对：编辑期间后台提炼/整理改过文件就拒绝覆盖
            "mtime": mtime,
            # 系统提示词实际注入的字数（超 4000 字保最新条目按行截断），页面据此提示
            "inject_chars": len(inject_text(text)),
            "digest_enabled": self.cfg.memory_digest,
            "maintain": {
                "global_enabled": st.global_enabled,
                "project_enabled": st.project_enabled,
                "interval_hours": st.interval_hours,
                "global_last": float(state.get("global_last") or 0),
                "project_last": float(
                    (state.get("project_last") or {}).get(str(self.working_dir or "")) or 0
                ),
            },
        }

    async def memory_save(self, text: str, base_mtime: float | None = None) -> dict:
        from ...tools.memory import MAX_MEMORY_FILE_CHARS, inject_text, memory_path

        p = memory_path()
        full = text[:MAX_MEMORY_FILE_CHARS]
        async with self._memory_io_lock:
            if base_mtime is not None and float(base_mtime) > 0:
                # 防覆盖：用户打开编辑器期间，归档提炼/定期整理可能已写入新条目；
                # 拿旧编辑整体落盘会把它们静默抹掉，mtime 对不上就拒绝
                try:
                    cur = round(p.stat().st_mtime, 3)
                except OSError:
                    cur = 0.0
                if cur != float(base_mtime):
                    raise RuntimeError(
                        "记忆在你编辑期间被更新过（归档提炼或定期整理已写入），"
                        "本次保存已阻止；请刷新本页重新编辑，以免丢掉新记忆"
                    )
            # 原子写（textio 同族）：写一半被杀不能留下半截记忆文件
            write_text_atomic(p, full)
        # 记忆注入系统提示词：保存后立刻对当前所有会话生效
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        try:
            mtime = round(p.stat().st_mtime, 3)
        except OSError:
            mtime = 0.0
        return {"saved": True, "path": str(p), "chars": len(text),
                "mtime": mtime, "inject_chars": len(inject_text(full))}

    async def memory_digest_save(self, enabled: bool) -> dict:
        """归档自动记忆总闸：写 config.toml 并热生效（下一次归档即按新值决定）。"""
        try:
            set_advanced_settings_in_config(memory_digest=bool(enabled))
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        return {"enabled": self.cfg.memory_digest}

    async def memory_maintain_save(
        self, global_enabled: bool | None = None,
        project_enabled: bool | None = None,
        interval_hours: int | None = None,
    ) -> dict:
        """定期整理设置：写 [memory_maintenance] 并热生效（巡检每轮读最新配置）。"""
        try:
            set_memory_maintenance_in_config(
                global_enabled=global_enabled,
                project_enabled=project_enabled,
                interval_hours=interval_hours,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        st = self.cfg.memory_maintenance
        return {
            "global_enabled": st.global_enabled,
            "project_enabled": st.project_enabled,
            "interval_hours": st.interval_hours,
        }

    # ---- 记忆地图：项目演化的可视化（时间线 + 主题图谱 + LLM 阶段摘要） ----

    MAP_SESSION_LIMIT = 500          # 时间线单次装载的会话上限（防超大项目撑爆载荷）
    MAP_FILE_LIMIT = 20              # 文件足迹 Top N
    MAP_HEAT_WEEKS = 260             # 热力图铺的周数上限（前端按底栏宽度自适应 26~260 周）
    MAP_MATERIAL_SESSIONS = 200      # 喂给摘要生成的会话条数上限
    MAP_MATERIAL_CHARS = 24_000      # 摘要材料的字符预算（超出丢最旧的会话）
    MAP_AUTO_MIN_SESSIONS = 8        # 自动生成：距上次摘要以来的新增会话数门槛
    MAP_AUTO_COOLDOWN_S = 24 * 3600  # 自动生成的冷却期

    async def _map_resolve_project(
        self, project_id: int | None, *, local: bool = True
    ) -> Project:
        """解析记忆地图的目标项目：缺省取当前项目；跨项目查看只给本机。

        会话标题/摘要/文件路径是跨项目的枚举面，与 session.search scope=all
        同一安全口径（远程客户端只看它被绑定到的当前项目）。
        """
        pid = int(project_id) if project_id else self._cur_project_id()
        if pid is None:
            raise RuntimeError("当前没有项目：先在侧栏「项目」区添加项目，才有演化可看")
        proj = await self.store.get_project(pid)
        if proj is None:
            raise RuntimeError("项目不存在或已被删除")
        if pid != self._cur_project_id() and not local:
            raise RuntimeError("查看其他项目的记忆地图只能在本机界面上操作")
        return proj

    @staticmethod
    def _map_range(params: dict) -> tuple[float, float]:
        """解析查询窗口：缺省近 90 天，显式 start_ts=0 表示不限起点（「全部」）；
        非法值夹回合法区间而不是报错。0 是合法取值，不能走 `or` 缺省（会被吞）。"""
        now = time.time()
        try:
            end = float(params.get("end_ts") or now)
        except (TypeError, ValueError):
            end = now
        raw_start = params.get("start_ts")
        if raw_start is None:
            start = now - 90 * 86400
        else:
            try:
                start = max(0.0, float(raw_start))  # 负数夹回 0（同为不限起点）
            except (TypeError, ValueError):
                start = now - 90 * 86400
        end = min(max(end, start), now + 86400)  # 未来最多放宽一天（时区误差兜底）
        return start, end

    def _map_checkpoint_metas(self, proj) -> list[dict]:
        """目标项目的检查点元数据（文件足迹数据源）。

        当前项目直接用常驻的 store 实例（启动时已加载，零盘扫）；跨项目查看
        才临时开一个只读实例按该项目的指纹目录加载。远程连接项目没有真实
        目录，直接给空（它本来也没有检查点）。
        """
        if proj.id == self._cur_project_id() and self.checkpoints is not None:
            return self.checkpoints.list_project_metas()
        if not proj.root_path or proj.root_path == self.store.REMOTE_PROJECT_PATH:
            return []
        try:
            root = self._checkpoint_root_for(Path(proj.root_path))
            return CheckpointStore(root).list_project_metas()
        except (OSError, ValueError):
            return []

    async def map_get(self, params: dict | None = None, *, local: bool = True) -> dict:
        """记忆地图载荷：一次装配时间线/热力图/文件足迹/事件/记忆/摘要全部维度。"""
        params = params or {}
        proj = await self._map_resolve_project(params.get("project_id"), local=local)
        start, end = self._map_range(params)
        sessions = await self.store.map_sessions_with_stats(proj.id, start, end)
        if len(sessions) > self.MAP_SESSION_LIMIT:
            sessions = sessions[-self.MAP_SESSION_LIMIT:]  # 保最新（已按时间升序）
        # 热力天数与时间线窗口解耦：格子周数随底栏宽度自适应，天数始终备足到
        # 上限跨度（外加一周对齐富余），否则远端格子因「没查到」画成假「无活动」
        heat_start = min(start, end - (self.MAP_HEAT_WEEKS * 7 + 7) * 86400)
        days = await self.store.map_day_stats(proj.id, heat_start, end)
        events = await self.store.map_project_events(proj.id, start, end)

        # 文件足迹：检查点 meta 按 path 聚合（count/首末时间/涉及会话），Top N
        files: dict[str, dict] = {}
        for cp in self._map_checkpoint_metas(proj):
            ts = float(cp.get("ts") or 0)
            if not (start <= ts <= end):
                continue
            sid = cp.get("session_id")
            for path_s in cp.get("paths") or []:
                ent = files.setdefault(path_s, {
                    "path": path_s, "count": 0, "first_ts": ts, "last_ts": ts,
                    "session_ids": [],
                })
                ent["count"] += 1
                ent["first_ts"] = min(ent["first_ts"], ts)
                ent["last_ts"] = max(ent["last_ts"], ts)
                if sid and sid not in ent["session_ids"]:
                    ent["session_ids"].append(sid)
        top_files = sorted(files.values(), key=lambda f: (-f["count"], f["path"]))
        for f in top_files:
            f["session_ids"] = f["session_ids"][:20]
        top_files = top_files[: self.MAP_FILE_LIMIT]

        # 全局记忆条目（跨项目，前端标「全局」徽记）：日期字符串与窗口的本地
        # 日期串直接比较，免去逐条转时区
        try:
            mem_text = memory_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            mem_text = ""
        start_day = time.strftime("%Y-%m-%d", time.localtime(start))
        end_day = time.strftime("%Y-%m-%d", time.localtime(end))
        memories = [
            e for e in parse_memory_entries(mem_text, 400)
            if start_day <= e["date"] <= end_day
        ][-30:]

        digests = await self.store.list_map_digests(proj.id)
        pname = proj.name or (Path(proj.root_path).name if proj.root_path else "")
        return {
            "project": {
                "id": proj.id,
                "name": pname or "未命名项目",
                "root_path": (proj.root_path if local and proj.root_path
                              != self.store.REMOTE_PROJECT_PATH else ""),
                "created_at": proj.created_at,
            },
            "range": {"start_ts": start, "end_ts": end},
            "sessions": sessions,
            "days": days,
            "events": events,
            "files": top_files,
            "memories": memories,
            "digests": digests,
            "generating": proj.id in self._map_generating,
            "config": {"auto_digest": bool(self.cfg.memory_map.auto_digest)},
        }

    async def map_generate(self, params: dict | None = None, *, local: bool = True) -> dict:
        """「生成演化摘要」：解析项目后起后台任务，立即返回（完成经事件广播）。

        按项目单飞：同一项目进行中再点直接让路，不叠加并发模型调用。
        """
        params = params or {}
        proj = await self._map_resolve_project(params.get("project_id"), local=local)
        if proj.id in self._map_generating:
            return {"started": False, "reason": "这个项目的摘要在生成中，稍等片刻"}
        if self.provider is None:
            raise RuntimeError("还没有可用的模型服务，先在 设置 · 模型服务 配置 API Key")
        if getattr(self.provider, "demo_mode", False):
            raise RuntimeError("演示模式没有真实模型，生成不了演化摘要；先在 设置 · 模型服务 配置")
        spawn_bg(self._map_generate_run(proj.id))
        return {"started": True, "project_id": proj.id}

    async def _map_generate_run(self, project_id: int) -> None:
        """生成演化摘要的主体：拼材料 → 调模型 → 宽容解析 → 校验落库 → 广播。

        与 _auto_title/_memory_digest 同一边界：失败不影响主流程，但会通过
        map_updated 事件把原因带给前端（按钮要能显示失败并可重试）。
        """
        self._map_generating.add(project_id)
        try:
            sessions = await self.store.map_sessions_with_stats(project_id, 0, time.time() + 1)
            if not sessions:
                self._ws_broadcast({
                    "kind": "map_updated", "project_id": project_id, "ok": False,
                    "message": "这个项目还没有会话，先聊出一点历史再来生成演化摘要",
                })
                return
            sessions = sessions[-self.MAP_MATERIAL_SESSIONS:]
            proj = await self.store.get_project(project_id)
            pname = (proj.name if proj and proj.name else "") or "未命名项目"
            prompt = self._map_build_material(pname, sessions)
            parts: list[str] = []
            async for ev in self.provider.stream([Message.user(prompt)], []):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            data = self._parse_map_digest_json("".join(parts))
            rows = self._map_digest_rows(data, sessions)
            if not rows:
                raise ValueError("模型没有给出有效的阶段划分")
            await self.store.replace_map_digests(project_id, rows)
            self._ws_broadcast({
                "kind": "map_updated", "project_id": project_id, "ok": True,
                "message": "演化摘要已生成：" + (
                    f"{sum(1 for r in rows if r['kind'] == 'phase')} 个阶段"
                    if any(r["kind"] == "phase" for r in rows) else "项目总览"
                ),
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 生成失败要让前端看到原因
            logger.warning("map digest generate failed: %s", e)
            self._ws_broadcast({
                "kind": "map_updated", "project_id": project_id, "ok": False,
                "message": f"演化摘要生成失败：{e}",
            })
        finally:
            self._map_generating.discard(project_id)

    @staticmethod
    def _map_build_material(project_name: str, sessions: list[dict]) -> str:
        """把会话流水压成摘要生成的材料：每会话一行编号条目 + 可选摘要。

        超出字符预算时丢最旧的会话（近期历史对「当前处于什么阶段」最有用）。
        编号 [n] 是模型回报阶段覆盖范围的句柄，不把 12 位会话 id 塞进提示词。
        """
        lines: list[str] = []
        used = 0
        # 从最新往旧收，超出预算即止；输出时恢复时间正序
        picked: list[str] = []
        picked_n = 0
        for s in reversed(sessions):
            day = time.strftime("%Y-%m-%d", time.localtime(s["created_at"]))
            bits = [f"[{len(sessions) - picked_n}] {day} 「{(s['title'] or '（未命名）')[:60]}」"]
            picked_n += 1
            stats = []
            if s["msg_count"]:
                stats.append(f"{s['msg_count']}条消息")
            if s["in_tokens"] or s["out_tokens"]:
                stats.append(f"{(s['in_tokens'] + s['out_tokens']) // 1000}k tokens")
            if s["tags"]:
                stats.append("标签:" + ",".join(s["tags"][:4]))
            if stats:
                bits.append(" ·" + " ·".join(stats))
            ln = bits[0] + ("".join(" " + b for b in bits[1:]))
            if s["summary"]:
                ln += f"\n    摘要：{s['summary'][:160]}"
            if used + len(ln) > MemoryMixin.MAP_MATERIAL_CHARS and picked:
                break
            picked.append(ln)
            used += len(ln) + 1
        lines = list(reversed(picked))
        return (
            f"你是项目演化记录员。下面是项目「{project_name}」在 SkySheep（AI Agent 工作台）"
            f"里的会话流水（按时间先后，[n] 是会话编号）。\n"
            "请把它划分成 2-5 个连续的演化阶段（每个阶段覆盖一段连续编号区间），"
            "并给项目一个一句话总览。\n\n"
            "只输出 JSON，不要 markdown 代码围栏，不要解释：\n"
            '{"overview": "不超过120字的项目总览",\n'
            ' "phases": [{"title": "阶段标题，不超过16字",\n'
            '   "summary": "这个阶段做了什么、为什么，不超过200字",\n'
            '   "highlights": ["要点1", "要点2"],\n'
            '   "topics": ["主题词1", "主题词2"],\n'
            '   "session_ids": [1, 2, 3]}]}\n'
            "要求：\n"
            "- 阶段按时间先后排列；session_ids 用方括号里的编号，"
            "所有编号都要被覆盖且不重叠\n"
            "- highlights 最多 5 条、每条不超过 40 字；topics 最多 6 个、每个不超过 12 字\n"
            "- 用中文；忠实于材料，不要编造没有出现的功能或事件\n\n"
            "会话流水：\n" + "\n".join(lines)
        )

    @staticmethod
    def _parse_map_digest_json(raw: str) -> dict:
        """宽容解析模型输出：剥代码围栏、掐首尾说明文字，取第一个完整 JSON 对象。"""
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("输出里没有找到 JSON")
        data = json.loads(text[i:j + 1])
        if not isinstance(data, dict):
            raise ValueError("JSON 不是对象")
        return data

    @staticmethod
    def _map_digest_rows(data: dict, sessions: list[dict]) -> list[dict]:
        """校验模型输出并折算成摘要行：编号映射回真实会话、补时间窗、去重叠。

        材料里的 [n] 对应 sessions 的第 n 条（1 起）。模型可能漏盖或重叠：
        编号已出现的阶段整体丢弃（时间上更晚的胜出没有依据，先到先得即可），
        编号越界的忽略。阶段时间窗取覆盖会话的 created_at/updated_at 极值。
        """
        by_idx = {i + 1: s for i, s in enumerate(sessions)}
        rows: list[dict] = []
        covered: set[int] = set()

        def _ints(raw_vals) -> list[int]:
            out: list[int] = []
            for v in raw_vals or []:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out

        overview = str(data.get("overview") or "").strip()
        phases = data.get("phases") if isinstance(data.get("phases"), list) else []
        phase_rows: list[dict] = []
        for ph in phases[:8]:
            if not isinstance(ph, dict):
                continue
            idxs = [n for n in dict.fromkeys(_ints(ph.get("session_ids"))) if n in by_idx]
            idxs = [n for n in idxs if n not in covered]
            if not idxs:
                continue
            covered.update(idxs)
            sub = [by_idx[n] for n in idxs]
            phase_rows.append({
                "kind": "phase",
                "title": str(ph.get("title") or "").strip()[:40] or "未命名阶段",
                "summary": str(ph.get("summary") or "").strip()[:500],
                "highlights": [str(h).strip()[:100] for h in (ph.get("highlights") or [])[:10]
                               if str(h).strip()],
                "topics": [str(t).strip()[:24] for t in (ph.get("topics") or [])[:12]
                           if str(t).strip()],
                "session_ids": [by_idx[n]["id"] for n in idxs],
                "start_ts": min(s["created_at"] for s in sub),
                "end_ts": max(s["updated_at"] for s in sub),
            })
        # 阶段按实际时间窗排序后重排编号顺序（模型偶尔乱序），总览行放最前
        phase_rows.sort(key=lambda r: r["start_ts"])
        if overview:
            all_ids = [r["session_ids"] for r in phase_rows]
            flat_ids = [sid for ids in all_ids for sid in ids] or [s["id"] for s in sessions]
            rows.append({
                "kind": "overview", "title": "项目总览", "summary": overview[:120],
                "highlights": [], "topics": [],
                "session_ids": flat_ids[:500],
                "start_ts": min((r["start_ts"] for r in phase_rows),
                                default=sessions[0]["created_at"]),
                "end_ts": max((r["end_ts"] for r in phase_rows),
                              default=sessions[-1]["updated_at"]),
            })
        rows.extend(phase_rows)
        return rows

    async def map_save_config(self, params: dict) -> dict:
        """记忆地图设置（自动生成开关）：写 [memory_map] 并热生效。"""
        auto = params.get("auto_digest")
        try:
            set_memory_map_config(auto_digest=None if auto is None else bool(auto))
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        return {"auto_digest": bool(self.cfg.memory_map.auto_digest)}

    async def _map_auto_digest_tick(self) -> None:
        """演化摘要的自动巡检：挂在记忆整理巡检里，不另起循环。

        开关默认关；开启后当前项目「距上次摘要超过冷却期且新增会话达到
        门槛」时后台补一次（与手动按钮共用单飞锁与失败广播）。
        """
        if not self.cfg.memory_map.auto_digest:
            return
        if self.provider is None or getattr(self.provider, "demo_mode", False):
            return
        pid = self._cur_project_id()
        if pid is None or pid in self._map_generating:
            return
        last = await self.store.latest_map_digest_ts(pid)
        if time.time() - last < self.MAP_AUTO_COOLDOWN_S:
            return
        if await self.store.count_project_sessions_since(pid, last) < self.MAP_AUTO_MIN_SESSIONS:
            return
        spawn_bg(self._map_generate_run(pid))
