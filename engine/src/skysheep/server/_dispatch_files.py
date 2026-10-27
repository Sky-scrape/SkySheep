"""WS handler（项目与文件域）：project / fs / snippets。自 app.py 逐字迁入，
行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ._dispatch_common import _WsMethod
from .backend import BUILTIN_SNIPPETS, ServerBackend


async def _h_project_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    projects = await backend.store.list_projects()
    # 拖动排序（分组视图）：有保存的顺序就按它来（见 Backend.ordered_projects）
    projects = backend.ordered_projects(projects)
    # 安全审查 C5：项目根目录的绝对路径是 B/C 族攻击的目标枚举器，
    # 远程客户端只需要名称与当前标记（切换/删除本已限本机）
    return {"projects": [
        {
            "id": p.id, "name": p.name,
            # 「远程连接」固定项目没有真实目录：root_path 报空，
            # 前端据此不提供切换/删除（点击组内会话直接打开）
            "root_path": "" if backend.is_remote_project(p) else (p.root_path if local else ""),
            "is_current": (backend.project is not None and p.id == backend.project.id),
        }
        for p in projects
    ]}


async def _h_project_switch(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.switch_project(str(params.get("path", "")))


async def _h_project_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_project(int(params.get("id", 0)))


def _project_task_scoped_params(params: dict, local: bool) -> dict:
    """非本机连接把任务清单读写收敛到当前项目（安全审查 L07）。

    project.task_list / project.task_add 接受任意 project_id 且此前远程可达，
    可跨项目读写任务清单（标题/详情是其他项目的数据面）；照 _map_resolve_project
    的收口写法，远程与记忆地图同一口径——只允许当前项目，显式传的其他
    project_id 一律剥掉。返回新 dict，不改调用方的参数对象；本机调用不受影响。
    """
    if local or params.get("project_id") is None:
        return params
    return {**params, "project_id": None}


async def _ensure_task_in_current_project(backend: ServerBackend, params: dict) -> None:
    """task_update / task_delete 只带任务 id：非本机先查归属再放行。

    任务不属于当前项目（或查无此任务）一律拒绝，不给枚举探测提供区分信号；
    本机调用不经过这里（分屏列跨项目管理任务清单的既有语义不变）。
    """
    try:
        tid = int(params.get("id") or 0)
    except (TypeError, ValueError):
        tid = 0
    task = await backend.store.get_project_task(tid)
    if task is None or task.get("project_id") != backend._cur_project_id():
        raise RuntimeError("只能操作当前项目的任务清单")


async def _h_project_task_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    if not local:
        params = _project_task_scoped_params(params, local)
    return await backend.project_task_list(params.get("project_id"))


async def _h_project_task_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    if not local:
        params = _project_task_scoped_params(params, local)
    return await backend.project_task_add(params)


async def _h_project_task_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    if not local:
        await _ensure_task_in_current_project(backend, params)
    return await backend.project_task_update(params)


async def _h_project_task_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    if not local:
        await _ensure_task_in_current_project(backend, params)
    return await backend.project_task_delete(params)


async def _h_project_instructions(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.get_instructions()


async def _h_project_save_instructions(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    bm = params.get("base_mtime")
    return await backend.save_instructions(
        str(params.get("text", "")),
        base_mtime=None if bm is None else float(bm),
    )


async def _h_fs_read(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.fs_read(params)


async def _h_fs_write(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.fs_write(params)


async def _h_fs_files(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.workspace_files()


async def _h_fs_open(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.open_workspace_file(str(params.get("path", "")))


async def _h_snippets_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # builtin 随响应下发：用户把指令删光时，前端 / 菜单用它兜底；
    # initials 供 ~ 菜单按拼音首字母过滤（后端算好，前端不引拼音库）
    rows = await backend.store.list_snippets()
    for r in rows:
        r["initials"] = backend.snippet_initials(str(r.get("name", "")))
    return {"snippets": rows, "builtin": BUILTIN_SNIPPETS}


async def _h_snippets_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    name = str(params.get("name", "")).strip()
    content = str(params.get("content", "")).strip()
    if not name or not content:
        raise RuntimeError("名称与内容不能为空")
    return {"snippet": await backend.store.add_snippet(name, content)}


async def _h_snippets_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    ok = await backend.store.update_snippet(
        int(params.get("id", 0)),
        str(params.get("name", "")).strip(),
        str(params.get("content", "")).strip(),
        None if "enabled" not in params else bool(params.get("enabled")),
    )
    if not ok:
        raise RuntimeError("快捷指令不存在")
    return {"updated": True}


async def _h_snippets_set_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"updated": await backend.store.set_snippet_enabled(
        int(params.get("id", 0)), bool(params.get("enabled"))
    )}


async def _h_snippets_polish(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.snippets_polish(params)


async def _h_snippets_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"deleted": await backend.store.delete_snippet(int(params.get("id", 0)))}


async def _h_snippets_used(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 插入使用上报：计数 +1 与最近使用时间（设置页展示用）；失败不影响插入
    return {"updated": await backend.store.mark_snippet_used(int(params.get("id", 0)))}


async def _h_snippets_reorder(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 拖拽排序提交整份顺序；上限兜底，防异常载荷
    ids = [int(i) for i in (params.get("ids") or [])][:500]
    return {"reordered": await backend.store.reorder_snippets(ids)}


async def _h_snippets_restore_builtin(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"added": await backend.restore_builtin_snippets()}


async def _h_snippets_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.export_snippets()


async def _h_snippets_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.import_snippets(params)


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "project.list": _WsMethod(_h_project_list),
    "project.switch": _WsMethod(_h_project_switch, local_only=True),
    "project.delete": _WsMethod(_h_project_delete, local_only=True),
    # 项目任务清单：此前接受任意 project_id 且远程可达（跨项目读写面，审查 L07）；
    # handler 内把非本机调用收敛到当前项目，照记忆地图 _map_resolve_project 口径
    "project.task_list": _WsMethod(_h_project_task_list),
    "project.task_add": _WsMethod(_h_project_task_add),
    "project.task_update": _WsMethod(_h_project_task_update),
    "project.task_delete": _WsMethod(_h_project_task_delete),
    "project.instructions": _WsMethod(_h_project_instructions),
    "project.save_instructions": _WsMethod(_h_project_save_instructions, local_only=True),
    "fs.read": _WsMethod(_h_fs_read),
    "fs.write": _WsMethod(_h_fs_write),
    "fs.files": _WsMethod(_h_fs_files),
    "fs.open": _WsMethod(_h_fs_open, local_only=True),
    "snippets.list": _WsMethod(_h_snippets_list),
    "snippets.add": _WsMethod(_h_snippets_add),
    "snippets.update": _WsMethod(_h_snippets_update),
    "snippets.set_enabled": _WsMethod(_h_snippets_set_enabled),
    "snippets.polish": _WsMethod(_h_snippets_polish),
    "snippets.delete": _WsMethod(_h_snippets_delete),
    "snippets.used": _WsMethod(_h_snippets_used),
    "snippets.reorder": _WsMethod(_h_snippets_reorder),
    "snippets.restore_builtin": _WsMethod(_h_snippets_restore_builtin),
    "snippets.export": _WsMethod(_h_snippets_export),
    "snippets.import": _WsMethod(_h_snippets_import),
}
