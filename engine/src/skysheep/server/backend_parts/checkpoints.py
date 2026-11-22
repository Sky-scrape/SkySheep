"""检查点（撤销本轮改动 / 审查 diff）：会话级检查点库解析、列表、回滚与归属校验。

D1 会话与运行时域（backend.py Mixin 拆分批 5a 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

随方法搬入的安全模型（不许放松）：
- store 按会话所属项目解析（安全审查 M08）：跨项目分屏会话的快照落它自己
  项目的目录树，读取侧必须取同一个 store，否则「撤销本轮改动」永远查不到；
- 归属校验（B12 / M04）：检查点 id 顺序可枚举，不属于授权会话的快照不能
  凭 id 恢复或对比（恢复是写盘原语，对比会返回文件内容）；
- 会话运行中拒绝回滚（与 truncate 同一守卫）。

模块级 `MAX_DIFF_CHARS` / `_decode_bytes` / `_unified_diff` /
`_checkpoint_diff_files` 随本文件落位（`_decode_bytes` 仅被
`_checkpoint_diff_files` 消费，故一并随迁；其余模块级定义不动）。重量级
同步磁盘活一律经 asyncio.to_thread 离环，调用点保持原样。
"""

from __future__ import annotations

import asyncio
import difflib
import os
from pathlib import Path

from ...core.checkpoints import CheckpointConflictError, CheckpointStore
from ...messages import Message

MAX_DIFF_CHARS = 8000


def _decode_bytes(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _unified_diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="改前", tofile="改后", lineterm=""
        )
    )


def _checkpoint_diff_files(cp: dict) -> list[dict]:
    """检查点「审查」的逐文件对比（同步函数，调用方用 to_thread 离开事件循环）。

    逐文件读磁盘现状 + difflib 差异计算都是重量级同步活，大检查点回滚/审查
    直接跑在事件循环上会把整个引擎（所有会话的流式输出）卡住数秒。
    """
    files: list[dict] = []
    for path_s, pre in cp["files"].items():
        try:
            cur: bytes | None = Path(path_s).read_bytes()
        except OSError:
            cur = None
        if b"\x00" in (pre or b"") or b"\x00" in (cur or b""):
            files.append({"path": path_s, "status": "binary", "diff": ""})
            continue
        if pre is None and cur is None:
            status, before, after = "gone", "", ""  # 新建的文件后来又被删了
        elif pre is None:
            status, before, after = "created", "", _decode_bytes(cur)
        elif cur is None:
            status, before, after = "deleted", _decode_bytes(pre), ""
        elif pre == cur:
            status, before, after = "unchanged", _decode_bytes(pre), _decode_bytes(cur)
        else:
            status, before, after = "modified", _decode_bytes(pre), _decode_bytes(cur)
        diff = "" if status in ("unchanged", "gone", "binary") else _unified_diff(before, after)
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n…（diff 过长，已截断）"
        files.append({"path": path_s, "status": status, "diff": diff})
    return files


class CheckpointsMixin:
    """检查点（checkpoints.list / checkpoints.restore / checkpoints.diff 的实现面）。

    方法与模块级 diff 助手自 backend.py 逐字搬入；无 __init__、无类级可变属性。
    """


    # ---- 检查点（撤销本轮改动） ----

    async def _checkpoint_store_for_session(self, session_id: str) -> CheckpointStore:
        """会话的检查点库：与保存侧同一套判定（安全审查 M08）。

        跨项目分屏会话的快照落它自己项目的目录树（runtime ctx.own 的
        checkpoints，backend.py 轮末保存同款判定），读取侧必须取同一个
        store——否则这些会话的「撤销本轮改动」永远查不到自己的快照，回滚
        安全网静默失效。runtime 还在时直接用 ctx.checkpoints（保存侧用的
        就是它）；runtime 已被淘汰/未建时按会话所属项目的工作目录重新解析
        （_checkpoint_store_for_root 进程内缓存单例，与保存侧同一实例）。
        解析失败（项目目录消失等）回落引擎当前项目库——该会话本就产生不了
        新快照，查不到即报「不存在」，不会跨库误读。同名检查点 id（各项目
        store 的 seq 各自从 0 起，cp1 重名是常态）因此天然按会话所属库消歧。
        """
        rt = self.runtimes.get(session_id)
        if rt is not None and rt.ctx is not None:
            return rt.ctx.checkpoints
        try:
            _, workdir = await self._session_project(session_id)
        except Exception:  # noqa: BLE001 - 见 docstring：解析失败回落当前项目库
            return self.checkpoints
        cur = self.working_dir
        same = (workdir is None and cur is None) or (
            workdir is not None and cur is not None
            and os.path.normcase(str(workdir)) == os.path.normcase(str(cur))
        )
        return self.checkpoints if same else self._checkpoint_store_for_root(workdir)

    async def list_checkpoints(self, session_id: str | None = None) -> dict:
        """列出会话的检查点（审查面板数据源）。

        session_id 缺省取活动会话（本机既有契约）；显式传入时按该会话解析。
        远程调用由 app 层传连接绑定的会话、未绑定为空串——空串直接回空列表
        fail closed，绝不回退全局活动会话（安全审查 M04，与 runs.list 同口径）。
        store 按会话所属项目取（M08，见 _checkpoint_store_for_session）。
        """
        sid = session_id if session_id is not None else (self.session.id if self.session else "")
        if not sid:
            return {"checkpoints": []}  # 无会话/远程未绑定：不给任何条目
        store = await self._checkpoint_store_for_session(sid)
        return {"checkpoints": store.list_for(sid)}

    async def restore_checkpoint(self, checkpoint_id: str, force: bool = False,
                                 session_id: str | None = None) -> dict:
        """把某轮的文件改动回滚到改前状态；并告知模型「文件已被回滚」。

        回滚前引擎会比对「快照保存之后文件有没有又被改过」（并行会话/任务
        写同一文件的场景）：有冲突且未 force 时不动磁盘，返回 conflict 结果，
        前端把冲突文件列给用户确认后再带 force=true 重试。force 只豁免冲突
        检查，不豁免会话归属校验（_check_checkpoint_ownership 先行，M04）。

        session_id 是授权会话（缺省活动会话，本机既有契约不变）：远程调用由
        app 层传连接绑定的会话（未绑定为空串→下方直接拒绝，调用方断言的 sid
        不参与）；检查点从授权会话所属项目的 store 读取（M08），同名 cp1 按
        库消歧——别的项目同 id 的快照不在查找路径上。
        """
        sid = session_id if session_id is not None else (self.session.id if self.session else "")
        if not sid:
            raise RuntimeError("当前没有可回滚的会话")
        store = await self._checkpoint_store_for_session(sid)
        # get/restore 都是重量级同步磁盘活（读 blob、全文件哈希比对、原子写回；
        # 单条上限 256MB），与 save 一样包进线程跑，大检查点不冻结事件循环。
        # CheckpointConflictError / KeyError 可正常穿过 to_thread 传回。
        cp = await asyncio.to_thread(store.get, checkpoint_id)
        if cp is None:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（每会话保留最近 50 轮，更早的会被淘汰）"
            )
        # 归属校验（安全审查 B12）：检查点 id 顺序可枚举，不属于授权会话的
        # 快照不能凭 id 恢复（否则别的项目的文件内容会被写回磁盘）
        await self._check_checkpoint_ownership(cp, sid)
        # 会话运行中拒绝回滚（与 truncate 同一守卫）：回滚会改写正在跑的
        # 历史并往 runtime 追加系统提示，轮末落库会把这条提示再插一遍
        cp_sid = (cp or {}).get("session_id")
        cp_rt = self.runtimes.get(cp_sid) if cp_sid else None
        if cp_rt and cp_rt.run_task and not cp_rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再回滚")
        # 目录清理的硬边界：回滚会顺手收掉本轮隐式建出来的空目录（写
        # docs/a.txt 带出的 docs/），但项目/工作目录本身永远不能是清理目标
        try:
            _, keep_root = await self._session_project(sid)
        except Exception:  # noqa: BLE001 - 解析不出就退化为「只按创建时间判」
            keep_root = None
        try:
            outcome = await asyncio.to_thread(
                store.restore, checkpoint_id, force=force, keep_root=keep_root
            )
        except CheckpointConflictError as e:
            return {
                "conflict": True,
                "checkpoint_id": checkpoint_id,
                "files": e.conflicts,
            }
        except KeyError:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（每会话保留最近 50 轮，更早的会被淘汰）"
            ) from None
        files: list[str] = outcome.get("restored") or []
        failed: list[dict] = outcome.get("failed") or []
        note = Message.user(
            "(系统提示) 用户执行了「撤销本轮改动」，以下文件已恢复到本轮改动前的状态："
            + ", ".join(files)
            + "。后续如需引用这些文件请先重新读取。"
            + (
                "；以下文件未能恢复（被占用或其它错误），请人工检查："
                + "; ".join(f"{it.get('path', '')}（{it.get('error', '')}）" for it in failed)
                if failed
                else ""
            )
        )
        # 系统补记：以 user 角色入历史（模型要知道文件已回滚），但不是用户
        # 说的话——前端据此不挂编辑重发/引用，避免误触截断会话
        note.system_note = True
        sid = (cp or {}).get("session_id") or sid
        agent = self.runtimes[sid].agent if sid and sid in self.runtimes else self.agent
        agent.history.append(note)
        if sid:
            await self.store.append_message(sid, note)
        return {"restored": checkpoint_id, "files": files, "failed": failed}

    async def _check_checkpoint_ownership(self, cp: dict,
                                          session_id: str | None = None) -> None:
        """检查点必须属于授权会话（B12：cp id 顺序可枚举）。

        授权会话：显式传入的 session_id（远程连接=连接绑定的会话，由 app 层
        传入；本机分屏列=前端显式指定的会话），缺省活动会话。同项目里其他
        会话的快照也不能凭枚举到的 id 恢复/对比——恢复是写盘原语，对比会
        返回文件内容。无会话归属的旧检查点（meta 里没写 session_id）无法
        校验，同样拒绝。
        """
        sid = (cp or {}).get("session_id") or ""
        authorized = session_id if session_id is not None else (self.session.id if self.session else "")
        if not sid or not authorized or sid != authorized:
            raise RuntimeError("该检查点不属于当前会话，无法校验归属")

    async def checkpoint_diff(self, checkpoint_id: str,
                              session_id: str | None = None) -> dict:
        """「审查」标签页：某个检查点里每个文件的 改前快照 vs 磁盘现状 的 unified diff。

        会话授权与 store 解析同 restore_checkpoint（M04/M08）：远程按连接绑定
        会话，检查点从该会话所属项目的 store 读取。
        """
        sid = session_id if session_id is not None else (self.session.id if self.session else "")
        if not sid:
            raise RuntimeError("当前没有可对比的会话")
        store = await self._checkpoint_store_for_session(sid)
        # get（读 blob）与逐文件对比（读磁盘 + difflib）都离环跑，见 restore_checkpoint
        cp = await asyncio.to_thread(store.get, checkpoint_id)
        if cp is None:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（每会话保留最近 50 轮，更早的会被淘汰）"
            )
        # 同 restore：凭枚举 id 不能读别的项目会话的文件快照（B12）
        await self._check_checkpoint_ownership(cp, sid)
        files = await asyncio.to_thread(_checkpoint_diff_files, cp)
        return {"id": checkpoint_id, "ts": cp["ts"], "files": files}
