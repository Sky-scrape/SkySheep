"""WS handler（会话库域）：session / checkpoint / term / diagnostics /
usage。自 app.py 逐字迁入，行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ._dispatch_common import _remote_scope, _WsMethod
from .backend import ServerBackend


def _gate_session_search(params: dict) -> str | None:
    scope = "all" if str(params.get("scope", "project")) == "all" else "project"
    if scope == "all":
        # 跨项目搜索是有意的本机产品功能，但远程客户端不该能借它枚举
        # 其他项目的会话 id/标题/片段（安全审查：scope=all 是 B 族的放大器）
        return "跨项目搜索只能在桌面端本机使用"
    return None


def _gate_session_list(params: dict) -> str | None:
    if params.get("all_projects"):
        return "跨项目会话列表只能在桌面端本机使用"
    return None


async def _h_session_search(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    scope = "all" if str(params.get("scope", "project")) == "all" else "project"
    return {
        "query": str(params.get("query", "")),
        "scope": scope,
        "results": await backend.store.search_messages(
            backend._cur_project_id(), str(params.get("query", "")), scope=scope
        ),
    }


async def _h_session_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 分组侧栏要全部项目的会话；跨项目的标题/摘要是枚举其他项目的放大器，
    # 与 session.search scope=all 同一安全口径：只给本机桌面端（安全审查 B 族）
    def _sess_brief(s) -> dict:
        return {
            "id": s.id, "title": s.title, "updated_at": s.updated_at,
            "pinned": bool(s.pinned), "project_id": s.project_id,
            "summary": s.summary,
            "tags": [t for t in str(s.tags or "").split(",") if t],
            "archived": bool(s.archived),
        }

    quick_sessions: list = []
    if params.get("all_projects"):
        grouped = await backend.store.list_sessions_by_project(50)
        sessions = [s for group in grouped.values() for s in group]
    else:
        sessions = (
            await backend.store.list_sessions(backend.project.id)
            if backend.project is not None
            else []
        )
        # 经典视图的快聊区块：快聊会话的 project_id 是 NULL，永远不会出现在
        # 「当前项目」列表里，不带这个字段侧栏就看不到它们。与 all_projects
        # 同一安全口径：快聊不属于当前项目，只给本机桌面端（远程客户端
        # 连字段都不下发，侧栏也就不用显示一个永远为空的区块）。
        if local:
            quick_sessions = await backend.store.list_quick_sessions()
    empty_count = await backend.store.count_empty_sessions(backend._cur_project_id())
    # 归档角标含快聊：侧栏归档弹窗能列快聊归档会话（见 list_archived_sessions），
    # 这里的计数必须同一口径，否则入口不出现，弹窗也就点不开。
    archived_count = await backend.store.count_archived_sessions(
        backend._cur_project_id(), include_projectless=True
    )
    out = {
        "empty_count": empty_count,
        "archived_count": archived_count,
        "sessions": [
            _sess_brief(s)
            for s in (sessions if params.get("all_projects") else sessions[:50])
        ],
    }
    if local and not params.get("all_projects"):
        out["quick_sessions"] = [_sess_brief(s) for s in quick_sessions]
    return out


async def _h_session_new(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.new_session(str(params.get("title", "")))


async def _h_session_new_task(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.create_task_chat()


async def _h_session_truncate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.truncate_session(params, local=local)


async def _h_session_fork(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.fork_session(params, local=local)


async def _h_session_activate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.activate_session(str(params.get("id", "")))


async def _h_session_resume(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.resume_session(str(params.get("id", "")))


async def _h_session_image(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 历史图片按需拉取（boot/历史只下占位，见 _msg_brief）；本机跨项目放行
    return await backend.session_image(params, local=local)


async def _h_session_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_session(str(params.get("id", "")), local=local)


async def _h_session_project_path(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_project_path(str(params.get("id", "")))


async def _h_session_peek(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.peek_session_messages(str(params.get("id", "")), local=local)


async def _h_messages_earlier(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 大会话历史窗口化的翻页半边：归属校验在 backend.session_messages_earlier
    # 里与 session.peek_messages 同口径执行（本机跨项目可读；远程按当前项目）
    return await backend.session_messages_earlier(params, local=local)


async def _h_session_model_switch(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_model_switch(
        str(params.get("id", "")),
        str(params.get("name", "")),
        str(params.get("model", "")),
    )


async def _h_session_model_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.session_model_get(str(params.get("id", "")))


async def _h_session_reasoning_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 先取 id 再做任何 await：空参/未知 id 立即报错（本机专属方法整表空参回归的快速失败路径）
    sid = str(params.get("id", "") or "")
    return await backend.session_reasoning_get(sid)


async def _h_session_reasoning_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    sid = str(params.get("id", "") or "")
    return await backend.session_reasoning_set(sid, str(params.get("effort", "auto")))


async def _h_session_reveal(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.reveal_session_project(str(params.get("id", "")))


async def _h_session_accept_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_accept_get(str(params.get("id", "")))


async def _h_session_accept_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_accept_set(
        str(params.get("id", "")), str(params.get("mode", "confirm"))
    )


async def _h_session_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.export_session(
        str(params.get("id", "")), fmt=str(params.get("fmt", "md"))
    )


async def _h_session_cleanup_empty(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cleanup_empty_sessions()


async def _h_session_rename(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.rename_session(
        str(params.get("id", "")), str(params.get("title", ""))
    )


async def _h_session_pin(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pin_session(
        str(params.get("id", "")), bool(params.get("pinned", False))
    )


async def _h_session_archive(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.archive_session(
        str(params.get("id", "")), bool(params.get("archived", False))
    )


async def _h_session_list_archived(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.list_archived_sessions()


async def _h_session_move(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    pid = params.get("project_id")
    return await backend.move_session(
        str(params.get("id", "")), int(pid) if pid is not None else None
    )


async def _h_session_tags(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_session_tags(
        str(params.get("id", "")), params.get("tags") or []
    )


async def _h_session_tags_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"tags": await backend.store.list_all_tags(backend._cur_project_id())}


async def _h_session_backups(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 安全审查 C4：备份列表带绝对路径，远程客户端只看名称/时间
    # （恢复动作本已限本机，列表仅供查看）
    out = backend.list_session_backups()
    if not local:
        out = {
            **out,
            "dir": "",
            "backups": [
                {**b, "path": ""} for b in out.get("backups", [])
            ],
        }
    return out


async def _h_session_restore_backup(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.restore_session_backup(str(params.get("name", "")))


async def _h_session_create_backup(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.create_session_backup()


async def _h_session_delete_backup(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_session_backup(str(params.get("name", "")))


def _checkpoint_scope(params: dict, local: bool) -> str | None:
    """checkpoint.* 的授权会话（M04/M08）。

    远程：收敛到连接绑定的会话（_remote_scope），未绑定回空串由调用方
    fail closed——检查点 id（cp{seq}）顺序可枚举，凭全局指针能枚举/对比/
    回滚别会话的文件快照。本机：前端显式指定的 session_id（跨项目分屏列的
    审查面板按列传 sid）优先，缺省 None=活动会话（本机既有语义不变；显式
    指定是他自己机器上点选的会话，与 delete 等本机操作的既有姿态一致）。
    """
    only = _remote_scope(local)
    if only is not None:
        return only
    sid = str(params.get("session_id") or "").strip()
    return sid or None


async def _h_checkpoint_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    only = _checkpoint_scope(params, local)
    if only == "":
        return {"checkpoints": []}  # 远程尚未绑定会话：不给任何条目
    return await backend.list_checkpoints(only)


async def _h_checkpoint_restore(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # restore 是写盘原语（把工作区文件覆盖回改前快照）：远程只允许回滚连接
    # 绑定会话自己的检查点，调用方断言的 sid / force 都不能越界（M04）。
    only = _checkpoint_scope(params, local)
    if only == "":
        raise RuntimeError("远程未绑定会话，无法回滚检查点")
    return await backend.restore_checkpoint(
        str(params.get("id", "")), force=bool(params.get("force", False)),
        session_id=only,
    )


async def _h_checkpoint_diff(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # diff 会返回改前文件内容：与 list/restore 同一套会话授权口径（M04）。
    only = _checkpoint_scope(params, local)
    if only == "":
        raise RuntimeError("远程未绑定会话，无法对比检查点")
    return await backend.checkpoint_diff(
        str(params.get("id", "")), session_id=only,
    )


async def _h_diagnostics_turn_breakdown(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 审查页「轮次诊断」：读结构化日志里的每轮耗时拆解（只读，不碰引擎状态）
    if not local:
        # 远程只看当前活动会话（安全审查 B13 同款收窄）：诊断行含时间/耗时/
        # 工具名/token，凭传入 sid 能翻别会话的日志，还能借 turns 空/非空
        # 探测某 id 是否在本机日志里出现过。活动指针为 None 时取空串——
        # 日志行都带真实 session_id，空串匹配不到任何行（探测面为零）
        sid = backend.session.id if backend.session else ""
        return await backend.turn_breakdown(sid, params.get("limit", 20))
    return await backend.turn_breakdown(
        str(params.get("session_id") or ""),
        params.get("limit", 20),
    )


async def _h_term_spawn(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    tid = str(params.get("term_id", "") or "")
    rows = int(params.get("rows", 24) or 24)
    cols = int(params.get("cols", 100) or 100)
    return backend.term_spawn(tid, rows, cols)


async def _h_term_input(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    tid = str(params.get("term_id", "") or "")
    rows = int(params.get("rows", 24) or 24)
    cols = int(params.get("cols", 100) or 100)
    return backend.term_input(tid, str(params.get("data", "")), rows, cols)


async def _h_term_resize(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    tid = str(params.get("term_id", "") or "")
    rows = int(params.get("rows", 24) or 24)
    cols = int(params.get("cols", 100) or 100)
    return backend.term_resize(tid, rows, cols)


async def _h_term_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # Ctrl+C 是收紧动作，远端也放行（否则远程发起的程序停不下来）；
    # 不带 term_id 时发给所有终端标签的前台进程
    return backend.term_stop(str(params.get("term_id", "") or ""))


async def _h_term_close(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 关闭终端标签：结束该标签的 shell 并丢弃执行槽
    return backend.term_close(str(params.get("term_id", "") or ""))


async def _h_usage_stats(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.usage_stats(params)


async def _h_usage_collab_report(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 只读报表：与 usage.stats 同姿态，远端照常可查（按项目口径收敛）
    return await backend.usage_collab_report(params)


async def _h_usage_pricing_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.usage_pricing_set(params)


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "session.search": _WsMethod(_h_session_search, local_gate=_gate_session_search),
    "session.list": _WsMethod(_h_session_list, local_gate=_gate_session_list),
    "session.new": _WsMethod(_h_session_new),
    "session.new_task": _WsMethod(_h_session_new_task),
    "session.truncate": _WsMethod(_h_session_truncate),
    "session.fork": _WsMethod(_h_session_fork),
    "session.activate": _WsMethod(_h_session_activate),
    "session.resume": _WsMethod(_h_session_resume),
    "session.image": _WsMethod(_h_session_image),
    "session.delete": _WsMethod(_h_session_delete),
    "session.project_path": _WsMethod(_h_session_project_path, local_only=True),
    "session.reveal": _WsMethod(_h_session_reveal, local_only=True),
    "session.peek_messages": _WsMethod(_h_session_peek),
    # 大会话历史翻页（只读）：与会话打开同口径校验归属，远程可用（当前项目内）
    "messages.earlier": _WsMethod(_h_messages_earlier),
    "session.model_switch": _WsMethod(_h_session_model_switch, local_only=True),
    "session.model_get": _WsMethod(_h_session_model_get, local_only=True),
    # 会话级思考强度（本机专属）：读不建 runtime，写换会话私有 provider
    # 会话级权限三档（本机专属）：只动该会话私有门，引擎默认档不受影响
    "session.accept_get": _WsMethod(_h_session_accept_get, local_only=True),
    "session.accept_set": _WsMethod(_h_session_accept_set, local_only=True),
    "session.reasoning_get": _WsMethod(_h_session_reasoning_get, local_only=True),
    "session.reasoning_set": _WsMethod(_h_session_reasoning_set, local_only=True),
    "session.export": _WsMethod(_h_session_export),
    "session.cleanup_empty": _WsMethod(_h_session_cleanup_empty),
    "session.rename": _WsMethod(_h_session_rename),
    "session.pin": _WsMethod(_h_session_pin),
    "session.archive": _WsMethod(_h_session_archive),
    "session.list_archived": _WsMethod(_h_session_list_archived),
    "session.move": _WsMethod(_h_session_move),
    "session.tags": _WsMethod(_h_session_tags),
    "session.tags_list": _WsMethod(_h_session_tags_list),
    "session.backups": _WsMethod(_h_session_backups),
    "session.restore_backup": _WsMethod(_h_session_restore_backup, local_only=True),
    "session.create_backup": _WsMethod(_h_session_create_backup, local_only=True),
    "session.delete_backup": _WsMethod(_h_session_delete_backup, local_only=True),
    "checkpoint.list": _WsMethod(_h_checkpoint_list),
    "checkpoint.restore": _WsMethod(_h_checkpoint_restore),
    "checkpoint.diff": _WsMethod(_h_checkpoint_diff),
    "diagnostics.turn_breakdown": _WsMethod(_h_diagnostics_turn_breakdown),
    # 终端是常驻 shell（用户手敲命令，不进权限门，见 TerminalManager 注释），
    # 所以它只在“用户就在这台机器面前”时成立。局域网模式下持有令牌的设备
    # 也能连 WS，若允许它调用这里，令牌就等于 shell 访问权限。
    "term.spawn": _WsMethod(_h_term_spawn, local_only=True, local_error="终端面板只能在本机上使用"),
    "term.input": _WsMethod(_h_term_input, local_only=True, local_error="终端面板只能在本机上使用"),
    "term.resize": _WsMethod(_h_term_resize, local_only=True, local_error="终端面板只能在本机上使用"),
    "term.stop": _WsMethod(_h_term_stop),
    "term.close": _WsMethod(_h_term_close, local_only=True),
    "usage.stats": _WsMethod(_h_usage_stats),
    # 协作成本看板：报表只读（远端可查）；价目写 config.toml，本机专属
    "usage.collab_report": _WsMethod(_h_usage_collab_report),
    "usage.pricing_set": _WsMethod(
        _h_usage_pricing_set, local_only=True,
        local_error="价目设置会写入配置，只能在桌面端本机执行"),
}
