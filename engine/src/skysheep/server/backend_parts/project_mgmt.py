"""工作项目切换与项目增删：重绑路径、软移除 / 恢复、设置页项目列表。

D1 会话与运行时域（backend.py Mixin 拆分批 5b 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

机械改写（唯一一处）：_toast_blocking 内的宿主静态自引用
`ServerBackend._toast_launch_uri()` → `PreferencesMixin._toast_launch_uri()`。
该静态助手定义在 D6 PreferencesMixin（backend_parts/preferences.py），本文件
不能反向 import backend（成环）；助手本身纯静态（按实例身份拼 skysheep://
激活 URI），行为逐字不变。

跨域写（设计 §3.3 雷区 `_teams`，批 6 已收敛）：_bind_project 的切项目中断
整块收在 D2 CollabBodyMixin 的 team_interrupt_for_project_switch()——含
「跨项目分屏列 own-ctx 豁免」过滤（rt.ctx.own 的会话不随主栏切项目失效）、
按 sid pop → _release_team_providers → 登记 `interrupt_reason="项目已切换，
团队已中断"` → 条件 _persist_team_state。本文件只调封装，不再碰 `_teams`
/ `_teams_interrupted`（设计 §5 风险 2）。内核 setup 也走 _bind_project
（connect_mcp=False 的启动路径）。

字段所有权：无 D1 自有字段（`_teams*` 归 D2；`instructions_file` /
`instructions_text` 属 D1 但由主类 __init__ 铺齐、开工路径写）。本 Mixin
无 __init__、无类级可变属性。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ...config import skysheep_home
from ...core.checkpoints import CheckpointStore
from ...core.hooks import HookRunner, hooks_from_config, load_raw_config
from ...core.prompt import load_project_instructions
from ...core.subagent import TaskManager
from ...messages import Message
from ...security.gate import PermissionGate
from ...session.store import Project
from ...skills import SkillLoader
from .preferences import PreferencesMixin


class ProjectMgmtMixin:
    """项目切换 / 删除 / 软移除 / 恢复与项目页列表（D1 批 5b 外迁件）。

    方法自 backend.py 逐字搬入；状态全部来自主类共享 self（无 __init__）。
    """

    # ---- 工作项目切换（应用内设定工作目录，对标 Claude Code /add-dir 等） ----

    async def _bind_project(self, target: Path | None, *, connect_mcp: bool = True) -> None:
        """把引擎整体绑定到一个工作目录（None = 释放项目，进入无项目态）。

        switch_project / 删除项目 / setup 共用这一条重绑路径：白名单（权限门）、
        项目技能、项目记忆、项目级 MCP、子代理工作目录、检查点、钩子全部跟着
        target 走；无项目态只有全局技能/全局 MCP/全局记忆可用，快照会话归属
        快聊（project_id 为 NULL）。模型服务是全局配置，不受影响。

        connect_mcp=False 只给启动路径（setup）用：项目绑定本身不同步连 MCP，
        连接由 setup 的后台任务统一做——同步连会把「服务就绪」拖过桌面端启动
        预算（挂死/慢的服务器各烧一个连接超时），而且 setup 随后还要整体重建
        manager 再连一遍，白费一倍进程。
        """
        # 旧项目的子代理还引用着旧工作目录，先全部停掉
        if self.tasks:
            self.tasks.cancel_all()

        self.working_dir = target
        self.project = (
            await self.store.get_or_create_project(str(target)) if target is not None else None
        )
        # 信任按项目记忆：换了目录必须丢掉旧实例，否则会沿用上一个项目的信任状态
        self._trust = None
        # 辅助对话的历史里带着旧项目的 cwd（系统消息只在历史为空时注入），
        # 换项目不清掉的话，模型仍以为还在上一个目录里干活
        self.aux_histories.clear()
        # 白名单按项目隔离：换项目 = 换一套规则；权限档位跨项目保持
        prev_accept = self.gate.auto_accept_write if self.gate else False
        prev_full = self.gate.auto_accept_all if self.gate else False
        self.gate = PermissionGate(store=self.store, project_id=self._cur_project_id(),
                                   working_dir=self.working_dir)
        # Mods 收紧查询接到新门上（切项目 = 换门；运行期改动由 _reload_mods 兜住）
        if self.mods is not None:
            self.gate.extra_confirm = self.mods.extra_confirm
        self.gate.auto_accept_write = prev_accept
        self.gate.auto_accept_all = prev_full
        await self.gate.load_project_rules()
        if self._base_agent is not None:
            self._base_agent.gate = self.gate

        self.skills = SkillLoader(
            global_dir=skysheep_home() / "skills",
            project_dir=self._project_skills_dir_if_trusted(),
            state_path=(target / ".skysheep" / "skills.json") if target else None,
            scope_path=skysheep_home() / "skills-scope.json",
            project_root=target,
        )
        self.skills.discover()

        self.instructions_file, self.instructions_text = (
            load_project_instructions(target) if target is not None else (None, "")
        )

        self.tasks = TaskManager(
            provider_factory=lambda: self.provider,
            working_dir=target,
            max_iterations=self.cfg.subagent_max_iterations,
            store=self.subagent_store,
            provider_resolver=self._subagent_provider,
            registry_resolver=self._subagent_registry,
            max_concurrent=self.cfg.subagent_max_concurrent,
            usage_recorder=self._record_subagent_usage,
            event_emitter=self._ws_broadcast,
            state_path=skysheep_home() / "subagent_tasks.json",
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
        )
        self.tasks.run_center = self.runs

        # 项目级 mcp.json 指向新目录 → 差量接入新目录的服务器；顺带用新技能/子代理重建完整注册表。
        # 启动路径（connect_mcp=False）跳过：连接统一走 setup 的后台任务，避免同步等待与双重连接
        if connect_mcp:
            await self._sync_mcp_changes()

        # 钩子与检查点跟项目走：钩子换工作目录，检查点换目录树，避免把
        # A 项目的文件快照回滚到 B 项目
        raw_cfg = load_raw_config()
        pre_rules, post_rules, stop_rules = hooks_from_config(raw_cfg)
        self.hooks = HookRunner(pre_rules, post_rules, working_dir=target,
                                stop_rules=stop_rules) \
            if (pre_rules or post_rules or stop_rules) else None
        # Mods 与钩子同位重建：工作目录变了，草稿落点/收紧声明语境随之刷新；
        # Agent 构造点（setup 的 base agent、_get_runtime）在之后读 self.mods
        self._reload_mods()
        # Claude 兼容模式同位重建（两者互斥：启用了 Claude 模式则 self.mods 留空）
        self._reload_mods_claude()
        self.checkpoints = CheckpointStore(root=self._checkpoint_root())

        # 旧项目的活动团队一并失效：成员 Agent 挂着旧项目的工具表与权限门，
        # 不能让切项目后的消息继续路由进旧团队（登记转「已中断」供 team.get 展示）。
        # 跨项目分屏列的团队除外：成员挂的是会话所属项目的工具表与权限门
        # （_own_ctx），不随主栏切换失效。整块收在 D2 CollabBodyMixin 的
        # team_interrupt_for_project_switch()（批 6）：本文件只调封装，不再碰
        # _teams / _teams_interrupted（设计 §5 风险 2）。
        self.team_interrupt_for_project_switch()
        # 旧项目的会话 runtime 全部失效：停任务、落空排队轮、释放、清空。
        # 排队轮的 Future 必须逐个落空（与 delete_session 同一口径）：否则那些
        # 发消息的请求要么等到被取消的轮在旧上下文里交棒空跑一轮后拿到裸
        # 内部错误，要么在取消落在 pipeline try 之前的窄竞态里永远挂死。
        # 显式落空给等待方一条可读错误，队列清空也让交棒找不到旧轮次。
        # 跨项目分屏列的 runtime 除外：它绑定会话所属项目（自己的 gate/钩子/
        # 检查点/子代理），主栏切项目不动它——这正是分屏并排看两个项目的意义。
        for rt in list(self.runtimes.values()):
            if rt.ctx is not None and rt.ctx.own:
                continue
            for item in list(rt.queue):
                item.fail(RuntimeError("项目已切换，本次请求未执行"))
            rt.queue.clear()
            t = rt.run_task
            if t and not t.done():
                t.cancel()
            self._forget_runtime(rt)
            self.runtimes.pop(rt.sid, None)
        for item in list(self._base_queue):
            item.fail(RuntimeError("项目已切换，本次请求未执行"))
        self._base_queue.clear()

        if self._base_agent is not None:
            self._base_agent.working_dir = target
            self._base_agent.hooks = self.hooks
            self._base_agent.set_system(self.compose_system())
            # 幸存的 runtime 只剩跨项目列：工作目录与系统提示词留在会话自己的
            # 项目里，不跟随主栏切换
            for rt in self.runtimes.values():
                if rt.ctx is not None and rt.ctx.own:
                    continue
                rt.agent.working_dir = target
                rt.agent.set_system(self.compose_system())

        # 记住当前项目（无项目态记 0）：重启后回到同一个状态
        await self._write_ui_prefs({"active_project": self._cur_project_id() or 0})

    async def switch_project(self, path: str) -> dict:
        """把引擎整体切到另一个工作目录：项目记录（含白名单）、项目技能、
        项目级 MCP、AGENTS.md 项目记忆、子代理工作目录全部跟着换，
        然后接上该项目最近的会话。模型服务是全局配置，不受影响。
        """
        if self._run_task and not self._run_task.done():
            raise RuntimeError("当前有任务正在运行，请先停止再切换项目")
        target = Path(str(path).strip().strip('"')).expanduser()
        if not target.is_absolute():
            raise RuntimeError("请使用文件夹的完整路径，例如 D:\\works\\demo")
        target = target.resolve()
        if not target.is_dir():
            raise RuntimeError("目录不存在：" + str(target))
        if self.project is not None and str(target) == str(self.working_dir):
            return {
                "switched": False,
                "path": str(self.working_dir),
                "project": self.project.name,
                "reason": "这已经是当前项目",
            }

        await self._bind_project(target)

        latest = await self.store.latest_session(self.project.id)
        if latest is not None:
            session_info = await self.resume_session(latest.id)
        else:
            self.session = None
            self._base_agent.load_history([Message.system(self.compose_system())])
            session_info = None
        return {
            "switched": True,
            "path": str(target),
            "project": self.project.name,
            "session": session_info,
            "mcp_warnings": self.mcp_warnings,
        }

    async def delete_project(self, project_id: int) -> dict:
        """彻底删除项目：项目记录、它的会话、白名单、任务清单与定时任务/流水线
        一并删除；电脑上的文件夹不受影响。

        只是「从列表里拿掉、保留会话」请用 remove_project（软移除，可恢复）。

        删除是真实的删除：当前项目删掉后**不再重建一条同目录的新记录**——
        还有别的项目就切到最近的一个，一个都不剩就进入无项目态（列表为空，
        快聊照常可用）。无项目态也是合法状态，重启后保持。
        「远程连接」也可删除：删掉后渠道再用时经 ensure_remote_project
        自动重建（下方重置实例内缓存，避免渠道会话挂到已删除的项目 id 上）。"""
        is_current = self.project is not None and project_id == self.project.id
        if is_current and self._run_task and not self._run_task.done():
            raise RuntimeError("当前有任务正在运行，请先停止再删除项目")
        # 接力级联收敛（接力模式设计 §7：清项目必须先停止并收敛关联接力，
        # 活动链不做级联隐删——停到终态再删，库内历史行保留只读）
        relay_converged = await self.relay_converge_for_deletion(
            project_id=project_id)
        proj = await self.store.get_project(project_id)
        removed = await self.store.delete_project(project_id)
        if not removed:
            raise RuntimeError("项目不存在，可能已被删除")
        # 该项目的会话已被级联删除：运行中心里它们的运行记录一并收敛，
        # 否则「打开会话」会指向库里已不存在的会话（整项目删除是本路径，
        # 不走 delete_session，清不到运行条目）。
        await self.prune_run_center_orphans()
        if proj is not None and self.is_remote_project(proj):
            self._remote_project_id_cache = None  # 渠道再用时经 ensure 重建
        # 该项目的检查点目录（改前文件快照）一并清掉：项目没了，留着它的
        # 完整文件快照只会占磁盘——且对应 store 已被替换，永远不会再被淘汰
        if proj is not None and proj.root_path:
            cp_root = self._checkpoint_root_for(Path(proj.root_path))
            await asyncio.to_thread(shutil.rmtree, cp_root, True)
        if not is_current:
            return {"removed": project_id, "was_current": False,
                    "relay_converged": relay_converged}
        # 删的是当前项目：不再为同一目录重建记录；还有别的项目就接上最近的，
        # 一个都不剩就进入无项目态（列表为空，快聊继续可用）。
        # 「远程连接」是固定项目（无真实目录），不计入候选——否则删完所有
        # 真实项目后会把工作目录绑到哨兵路径上
        remaining = [p for p in self.ordered_projects(await self.store.list_projects())
                     if not self.is_remote_project(p)]
        if remaining:
            next_path = remaining[0].root_path
            await self._bind_project(Path(next_path))
            self.session = None
            latest = await self.store.latest_session(self.project.id)
            if latest is not None:
                await self.resume_session(latest.id)
            else:
                self._base_agent.load_history([Message.system(self.compose_system())])
            return {
                "removed": project_id, "was_current": True,
                "switched_to": {"path": str(self.project.root_path), "name": self.project.name},
                "relay_converged": relay_converged,
            }
        await self._bind_project(None)
        self.session = None
        self._base_agent.load_history([Message.system(self.compose_system())])
        return {"removed": project_id, "was_current": True, "switched_to": None,
                "relay_converged": relay_converged}

    async def remove_project(self, project_id: int) -> dict:
        """软移除项目：只从侧栏/项目选择器隐藏，会话、白名单、任务全部保留。

        与 delete_project 的区别：项目行还在（removed_at > 0），已移除列表可恢复，
        会话仍可被搜索命中；磁盘文件夹不受影响。
        """
        is_current = self.project is not None and project_id == self.project.id
        if is_current and self._run_task and not self._run_task.done():
            raise RuntimeError("当前有任务正在运行，请先停止再移除项目")
        proj = await self.store.get_project(project_id)
        if proj is None:
            raise RuntimeError("项目不存在，可能已被移除")
        if not await self.store.hide_project(project_id):
            raise RuntimeError("项目已经被移除")
        if self.is_remote_project(proj):
            self._remote_project_id_cache = None
        if not is_current:
            return {"removed": project_id, "was_current": False}
        # 当前项目被移除：切到最近的一个仍显示的项目，或者进入无项目态。
        # 不删任何会话；隐藏项目的会话仍可通过搜索打开，恢复项目后回到侧栏。
        remaining = [p for p in self.ordered_projects(await self.store.list_projects())
                     if not self.is_remote_project(p)]
        if remaining:
            next_path = remaining[0].root_path
            await self._bind_project(Path(next_path))
            self.session = None
            latest = await self.store.latest_session(self.project.id)
            if latest is not None:
                await self.resume_session(latest.id)
            else:
                self._base_agent.load_history([Message.system(self.compose_system())])
            return {
                "removed": project_id, "was_current": True,
                "switched_to": {"path": str(self.project.root_path), "name": self.project.name},
            }
        await self._bind_project(None)
        self.session = None
        self._base_agent.load_history([Message.system(self.compose_system())])
        return {"removed": project_id, "was_current": True, "switched_to": None}

    async def restore_project(self, project_id: int) -> dict:
        """恢复软移除的项目到侧栏/项目选择器（不切换当前项目）。"""
        proj = await self.store.get_project(project_id)
        if proj is None:
            raise RuntimeError("项目不存在，可能已被彻底删除")
        if not await self.store.restore_project(project_id):
            raise RuntimeError("项目没有被移除")
        if self.is_remote_project(proj):
            self._remote_project_id_cache = None
        return {"restored": project_id, "name": proj.name}

    async def project_admin_list(self) -> dict:
        """设置·项目页：全部项目（含已移除）+ 会话数 + 最后活动时间。"""
        rows = await self.store.project_admin_list()
        active, removed = [], []
        for r in rows:
            proj = Project(r["id"], r["root_path"], r["name"], r["created_at"])
            remote = self.is_remote_project(proj)
            item = {
                "id": r["id"], "name": r["name"],
                "root_path": "" if remote else r["root_path"],
                "is_remote": remote,
                "is_current": self.project is not None and r["id"] == self.project.id,
                "session_count": r["session_count"],
                "last_active": r["last_active"],
                "removed_at": r["removed_at"],
            }
            (removed if r["removed_at"] > 0 else active).append(item)
        return {"projects": active, "removed": removed}

    # ---- 系统通知（app.notify 的阻塞 toast 底座） ----

    @staticmethod
    def _toast_blocking(title: str, body: str) -> None:
        from winotify import Notification, audio

        launch = PreferencesMixin._toast_launch_uri()
        toast = Notification(app_id="SkySheep", title=title, msg=body, launch=launch)
        toast.set_audio(audio.Silent, loop=False)
        toast.show()
