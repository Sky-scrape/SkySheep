"""会话生命周期：新建/打开/激活/截断/归属校验、删除/改名/置顶/归档、会话库备份与恢复。

从 backend.py 按职责注释整段搬入。方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import sqlite3

from ...messages import Message


class LifecycleMixin:
    """会话生命周期操作与会话库备份恢复。

    方法自 backend.py 按职责注释逐字搬入。
    """

    async def new_session(self, title: str = "") -> dict:
        # 无项目态新建的会话归入快聊（project_id 为 NULL）：没有项目可归属。
        # title：前端在空标签上预命名时随创建一起落库（标签命名功能的懒创建路径），
        # 带了名字就记为手动命名，首轮的自动标题不会再覆盖它。
        title = (title or "").strip()[:80]  # 预命名同 rename 的限长口径
        self.session = await self.store.create_session(self._cur_project_id(), title)
        if title:
            self._manually_named.add(self.session.id)
        # 标记为「新会话」：若设了新会话默认模型，首次建 runtime 时用专用 provider
        self._default_model_sessions.add(self.session.id)
        self._get_runtime(self.session.id)  # 预建 runtime（自带系统提示词）
        return {"id": self.session.id, "title": self.session.title, "summary": ""}

    async def create_task_chat(self) -> dict:
        """新建一个不绑定任何文件夹的「任务」会话（侧栏「任务」分组的 ＋）。

        只落库、不切换当前会话——前端拿到 id 自己打开（走普通会话激活路径）。
        """
        s = await self.store.create_session(None)
        return {"id": s.id, "title": s.title}

    async def open_initial_session(self) -> dict | None:
        """启动时接着上次的会话继续；完全没历史则不创建（懒创建：发第一条消息时才落库），
        避免每次启动都堆积空会话。无项目态接快聊最近的会话（latest_session(None)
        的语义就是 project_id IS NULL）。

        恢复目标优先 ui.json 的 session_active（上次激活的标签）；没有/失效才
        落到「最近会话」。标签列表（session_tabs）由前端按 snapshot 的 open_tabs
        恢复——后端只负责把活动指针指对，不代拉历史。"""
        prefs = self._read_ui_prefs()
        remembered = prefs.get("session_active")
        if isinstance(remembered, str) and remembered:
            try:
                return await self.resume_session(remembered)
            except Exception:
                pass  # 已删/跨项目：静默落回最近会话
        latest = await self.store.latest_session(self._cur_project_id())
        if latest is not None:
            return await self.resume_session(latest.id)
        self.session = None
        return None

    async def cleanup_empty_sessions(self) -> dict:
        """删除本项目下没有任何消息的空会话（保留当前会话与置顶会话）。

        无项目态清的是快聊的空会话（delete_empty_sessions(None) 的口径）。
        removed_ids 带回被删的会话 id：前端把打开着的对应标签一并收掉。"""
        keep = self.session.id if self.session else None
        removed_ids = await self.store.delete_empty_sessions(self._cur_project_id(), keep_id=keep)
        return {"removed": len(removed_ids), "removed_ids": removed_ids}

    async def _get_owned_session(self, session_id: str):
        """取属于当前项目的会话；不存在或属于其他项目一律报错。

        安全边界（安全审查 B 族）：store.get_session 只按 id 查询，所有
        按会话 id 的远程操作（chat.send/refs/export/delete/元数据…）必须
        先过这里，否则别的项目的会话会被挂进当前项目的工作目录与权限门下。
        报错不区分「不存在/无权」，避免给枚举探测提供区分信号。

        快聊会话（project_id IS NULL）不属于任何项目，却出现在侧栏的常驻
        「快聊」分组里，用户理应能像普通会话一样点开与删除：只按当前项目
        校验会让它们全部报「session not found」（列表看得见、点不动）。
        这里显式承认快聊的开放归属——它不带任何项目上下文，挂进当前项目的
        工作目录不构成跨项目越权；其他项目的会话仍然照旧拒绝。
        """
        pid = self.project.id if self.project is not None else None
        sess = await self.store.get_session_for_project(session_id, pid)
        if sess is None:
            # 快聊会话在项目查询下必然为 None：再确认它确实是无项目会话，
            # 而不是「属于别的项目」。两者放行的只有前者。
            sess = await self.store.get_session_for_project(session_id, None)
        if sess is None:
            # 「远程连接」项目的会话同理开放归属：渠道对话在侧栏可见可点，
            # 但它不属于任何桌面工作目录，挂进当前项目的门控不构成跨项目越权
            # （渠道会话的 runtime 自带 ChannelGate，不走这里的默认门控）。
            remote = await self._remote_project_id()
            if remote is not None:
                sess = await self.store.get_session_for_project(session_id, remote)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        return sess

    async def truncate_session(self, params: dict) -> dict:
        """消息级回退：为「重新生成 / 编辑重发」截断历史。

        mode=regen  ：删掉锚点（默认最后一条 user）之后的所有消息，保留用户消息；
        mode=edit   ：连锚点消息一起删（随后用户编辑后重发）。
        会话正在运行时拒绝（避免与进行中的 turn 互相踩踏）。
        """
        sid = str(params.get("id", "") or (self.session.id if self.session else ""))
        if not sid:
            raise RuntimeError("missing session id")
        rt = self.runtimes.get(sid)
        if rt and rt.run_task and not rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再操作")
        await self._get_owned_session(sid)

        seq = params.get("seq")
        mode = str(params.get("mode", "regen"))
        if seq is not None:
            pivot = int(seq)
        else:
            pivot = await self.store.find_last_user_seq(sid)
            if pivot is None:
                raise RuntimeError("会话里没有可回退的用户消息")
        # 校验锚点是 user 消息（regen 的语义是「重跑这条用户消息」）
        anchor = await self.store.get_message_at(sid, pivot)
        if anchor is None:
            raise RuntimeError("锚点消息不存在")
        if mode == "regen" and anchor["role"] != "user":
            # 自动回退到它前面最近的 user 消息
            rows = await self.store.search_messages_by_seq(sid, pivot, role="user")
            if rows is None:
                raise RuntimeError("锚点之前没有用户消息")
            pivot = rows
            anchor = await self.store.get_message_at(sid, pivot)

        include = mode == "edit"
        deleted = await self.store.truncate_from(sid, pivot, include_self=include)
        # 同步 runtime 内的历史（存在则从存储重载）
        if sid in self.runtimes:
            await self._reload_agent_history(self.runtimes[sid].agent, sid)
        # 带 id：WS 层按返回里的 id 跟踪连接当前交互的会话（会话不变，仍是它）
        out = {"id": sid, "deleted": deleted, "pivot_seq": pivot, "mode": mode}
        if mode == "edit":
            out["text"] = anchor["text"]
        return out

    async def activate_session(self, session_id: str) -> dict:
        """只切活动指针，不重载历史（标签切换用；runtime 已存在时不做任何重活）。"""
        sess = await self._get_owned_session(session_id)
        self.session = sess
        # 启动恢复：记住当前激活的标签，下次启动回到它。失败静默（偏好写不进
        # 不影响切换本身）。
        try:
            await self._write_ui_prefs({self.SESSION_ACTIVE_KEY: session_id})
        except Exception:
            pass
        if session_id not in self.runtimes:
            rt = self._get_runtime(session_id)
            await self._reload_agent_history(rt.agent, session_id)
        return {"id": sess.id, "title": sess.title}

    async def session_image(self, params: dict) -> dict:
        """按 (会话, seq, 图片序号) 取一张历史图片的 base64。

        历史消息里的图片只下发占位（见 _msg_brief），前端滚到可见时才来取——
        刷新/切会话不再为整段历史里的原图付几十 MB 的传输与内存。
        """
        sid = str(params.get("session_id", "") or "")
        seq = int(params.get("seq", 0) or 0)
        index = int(params.get("index", 0) or 0)
        await self._get_owned_session(sid)  # 归属校验：跨项目会话按不存在拒绝
        m = await self.store.get_message_at(sid, seq)
        if m is None:
            raise RuntimeError("消息不存在")
        imgs = [b for b in m.content if getattr(b, "type", "") == "image"]
        if index < 0 or index >= len(imgs):
            raise RuntimeError("图片不存在")
        b = imgs[index]
        return {"media_type": b.media_type, "data": b.data}

    async def _switch_after_removal(self) -> dict | None:
        """当前会话被删除/移走后：优先切到最近的其他会话；一个都不剩就回到"待新建"状态。"""
        latest = await self.store.latest_session(self._cur_project_id())
        if latest is not None:
            await self.resume_session(latest.id)
            return {"id": latest.id, "title": latest.title}
        self.session = None
        self._base_agent.load_history([Message.system(self.compose_system())])
        return None

    async def delete_session(self, session_id: str) -> dict:
        # 归属校验：别的项目的会话不能凭枚举到的 id 删除（安全审查 B6）
        await self._get_owned_session(session_id)
        # 先停掉该会话正在跑的 turn，再清理 runtime，最后删数据
        self.cancel_run(session_id)
        rt = self.runtimes.pop(session_id, None)
        was_active = self.session is not None and self.session.id == session_id
        if rt is not None:
            # 排队中的轮次随 runtime 一起消失，Future 必须逐个落空：
            # 不然那些发消息的请求永远等不到响应（请求挂死 + 协程泄漏）
            for item in list(rt.queue):
                item.fail(RuntimeError("会话已删除"))
            rt.queue.clear()
            if was_active and self._base_queue:
                # 懒创建窗口排进基底队列的消息同属这个会话，一并落空
                for item in list(self._base_queue):
                    item.fail(RuntimeError("会话已删除"))
                self._base_queue.clear()
            self._forget_runtime(rt)
            task = rt.run_task
            if task is not None and not task.done():
                # 等被取消的轮次收尾（含 shield 保护的落库）跑完再删行：
                # 落库在取消后仍会继续执行，删早了消息会插在删除之后，留下孤儿行
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10)
                except Exception:  # noqa: BLE001 - 超时/任务异常都不拦删除本身
                    pass
        # 二次取消窗口：主轮任务可能已经结束，而被停轮的后台落库还在独立任务
        # 里跑——这里等的必须是那份后台落库本身（等主任务等不到它），不然消息
        # 插在删除之后留下孤儿行（FK 未启用，插得进去）。定时任务/流水线的本地
        # runtime 不在 self.runtimes 里，这份等待不能放在上面的 rt 分支内。
        pt = self._persist_tasks.get(session_id)
        if pt is not None and not pt.done():
            try:
                await asyncio.wait_for(asyncio.shield(pt), timeout=10)
            except Exception:  # noqa: BLE001 - 超时/任务异常都不拦删除本身
                pass
        await self.store.delete_session(session_id)
        # 检查点（改前文件快照）随会话一起清：会话没了，快照不该继续占磁盘
        await asyncio.to_thread(self.checkpoints.forget_session, session_id)
        self._manually_named.discard(session_id)
        self._default_model_sessions.discard(session_id)
        switched = await self._switch_after_removal() if was_active else None
        # 广播给其它连接（另一窗口 / 手机端）：删掉的会话从列表与标签里消失
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "deleted": True})
        return {
            "deleted": session_id,
            "switched_to": switched,
            "new_active": self.session.id if self.session else None,
        }

    async def rename_session(self, session_id: str, title: str) -> dict:
        await self._get_owned_session(session_id)
        title = title.strip()[:80]  # 限长：超长标题撑爆侧栏/标签布局
        if not title:
            raise RuntimeError("标题不能为空")
        await self.store.set_title(session_id, title)
        # 用户手改过名字：自动标题不再覆盖（含首轮在跑时才改名的竞态窗口）
        self._manually_named.add(session_id)
        if self.session and self.session.id == session_id:
            self.session.title = title
        # 广播给其它连接（另一窗口 / 手机端），让它们的标签与侧栏同步改名
        for ws_emit in list(self.ws_emitters):
            try:
                await ws_emit({"kind": "session_updated",
                               "session_id": session_id, "title": title})
            except Exception:
                pass
        return {"id": session_id, "title": title}

    async def pin_session(self, session_id: str, pinned: bool) -> dict:
        sess = await self._get_owned_session(session_id)
        await self.store.set_pinned(session_id, pinned)
        # 广播给其它连接：另一窗口的侧栏同步置顶位（列表整条刷新拿新状态）
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title})
        return {"id": session_id, "pinned": pinned}

    async def archive_session(self, session_id: str, archived: bool) -> dict:
        """归档/取消归档：归档后会话从侧栏与搜索里消失，可在归档弹窗恢复。

        归档（且此前未归档、未提炼过）时后台自动提炼用户记忆：归档通常意味着
        「这事完了」，是判断哪些信息值得长期记住的自然时机；提炼失败静默，
        不影响归档本身。提炼过一次就不再重来（取消归档再归档也不重复花钱）。
        """
        sess = await self._get_owned_session(session_id)
        already = bool(sess.archived)
        await self.store.set_archived(session_id, archived)
        if archived and not already \
                and not await self.store.get_session_memory_digested(session_id):
            self._schedule_memory_digest(session_id)
        # 广播给其它连接：归档/恢复后另一窗口的侧栏与搜索同步隐/现。
        # archived 标记供前端把该会话的标签一并收掉——归档＝这条对话收摊，
        # 侧栏行与标签栏保持同步（恢复后从归档弹窗重新点开即可）。
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title,
                            "archived": archived})
        return {"id": session_id, "archived": archived}

    # ---- 会话库备份：列出 / 手动备份 / 删除 / 恢复 ----

    def _backup_busy_message(self, action: str) -> str | None:
        """备份/恢复共用的预检：还有会写库的轮次在跑时返回可读原因（None = 放行）。

        预检不能只扫 self.runtimes：定时任务与流水线节点用本地 SessionRuntime
        跑、不注册进 runtimes（运行窗口分钟级，比交互轮长得多），懒创建窗口
        还有基底占位 _base_run_task；渠道轮已注册进 runtimes，天然被覆盖。
        checkpoint 与 copy 之间若还有并发写，会拷出撕裂的库文件。
        """
        if self._base_run_task is not None and not self._base_run_task.done():
            return f"有会话正在启动，稍候再{action}"
        for rt in self.runtimes.values():
            if rt.run_task and not rt.run_task.done():
                return f"还有会话正在运行，先停止（Esc）或等它结束再{action}"
        if self._cron_running:
            return f"有定时任务正在运行，等它结束再{action}"
        if self._pipeline_running:
            return f"有流水线节点正在运行，等它结束再{action}"
        return None

    def list_session_backups(self) -> dict:
        """可恢复的会话库备份（含"当前"一项，便于对照时间）。"""
        items = self.store.list_backups()
        return {
            "backups": items,
            "dir": str(self.store.backup_dir()),
            "keep": self.store.BACKUP_KEEP,
        }

    async def create_session_backup(self) -> dict:
        """手动备份会话库（设置 · 关于的「立即备份」）。

        与恢复同一道防线：有会话在跑时不动手——checkpoint 与 copy 之间若还有
        并发写，可能拷出撕裂的库文件。
        """
        busy = self._backup_busy_message("备份")
        if busy:
            raise RuntimeError(busy)
        try:
            return await self.store.backup_now()
        except OSError as e:
            raise RuntimeError(f"备份失败：{e}") from None

    async def delete_session_backup(self, name: str) -> dict:
        """删除单份会话库备份（文件名校验在 store 层，与恢复同一套）。"""
        try:
            return await self.store.delete_backup(str(name or "").strip())
        except (OSError, ValueError, FileNotFoundError) as e:
            raise RuntimeError(f"删除失败：{e}") from None

    async def restore_session_backup(self, name: str) -> dict:
        """从备份恢复会话库：先关库、换文件、重开，再重建内存状态。

        恢复后所有会话 runtime 作废（历史与消息 id 都可能对不上），
        重新挂到最新会话上；前端收到结果后重载列表即可。
        """
        name = str(name or "").strip()
        if not name:
            raise RuntimeError("缺少备份文件名")
        busy = self._backup_busy_message("恢复")
        if busy:
            raise RuntimeError(busy)
        try:
            result = await self.store.restore_backup(name)
        except (OSError, ValueError, FileNotFoundError, sqlite3.DatabaseError) as e:
            # 坏备份（撕裂/垃圾字节）报的是 sqlite3.DatabaseError，也要转成可读
            # 错误（store 侧的恢复校验由 persist 组负责）。失败后把 store 连接
            # 兜回来：restore 流程先关连接再换文件，半途失败连接可能停在「已关」
            # 状态，不兜回的话之后所有会话功能都会跟着瘫
            detail = f"恢复失败：{e}"
            if getattr(self.store, "_db", None) is None:
                try:
                    await self.store.connect()
                except Exception as re_err:  # noqa: BLE001 - 兜底失败如实附在错误里
                    detail += f"；会话库重开失败：{re_err}"
            raise RuntimeError(detail) from None

        # 内存状态全部重建：旧 runtime 的历史/消息 seq 都可能与新库不一致
        for rt in list(self.runtimes.values()):
            self._forget_runtime(rt)
        self.runtimes.clear()
        # 懒创建毫秒级窗口可能仍有请求排在基底队列：预检已过但恢复动作进行中
        # 插进来的请求必须逐个落空（与 _bind_project 同一口径），只 clear 会让
        # 那些 Future 永远不 resolve（请求挂死）
        for item in list(self._base_queue):
            item.fail(RuntimeError("会话库已恢复，本次请求未执行"))
        self._base_queue.clear()
        self.session = None
        # 项目列表可能也随库回退了（备份里的项目集合是当时的样子）
        projects = await self.store.list_projects()
        cur = next(
            (p for p in projects if str(p.root_path).lower() == str(self.working_dir).lower()),
            None,
        )
        if cur is None:
            cur = await self.store.get_or_create_project(str(self.working_dir), self.working_dir.name)
        self.project = cur
        info = await self.open_initial_session()
        self._base_agent.load_history([Message.system(self.compose_system())])
        return {
            "restored": result["restored"],
            "safety_copy": result["safety_copy"],
            "session": info,
            "projects": [{"id": p.id, "name": p.name, "path": p.root_path} for p in projects],
        }
