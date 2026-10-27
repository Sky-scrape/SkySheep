"""WS handler（偏好与治理域）：ui / trust / memory / map / advanced /
retention / storage / archive / browser。自 app.py 逐字迁入，行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ._dispatch_common import _WsMethod, logger
from .backend import ServerBackend


async def _h_ui_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.get_ui_prefs()


async def _h_trust_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.trust_status()


async def _h_trust_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 清单含全部项目的绝对路径：与 session.backups 同理，只给本机界面
    return backend.trust_list()


async def _h_trust_revoke_path(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 撤销信任是收紧动作，远端也允许（避免被远程锁死在信任态）
    return await backend.trust_revoke_path(str(params.get("path", "")))


async def _h_trust_grant(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 信任一个项目 = 允许执行它自带的本地命令，只能由本机用户在界面上确认
    return await backend.trust_grant()


async def _h_trust_revoke(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 收回信任是收紧防护，远调用也允许（避免被远程锁死在信任态）
    return await backend.trust_revoke()


async def _h_ui_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    prefs = dict(params.get("prefs") or {})
    if not local and "accept_edits" in prefs:
        # 远端不能间接打开自动写入档（permission.set_mode 已拦，这里再堵一次）
        prefs.pop("accept_edits")
        logger.warning("远程客户端尝试通过 ui.save 设置 accept_edits，已忽略")
    return await backend.save_ui_prefs(prefs)


async def _h_memory_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_get()


async def _h_memory_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    bm = params.get("base_mtime")
    return await backend.memory_save(
        str(params.get("text", "")),
        base_mtime=None if bm is None else float(bm),
    )


async def _h_memory_maintain_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    gi, pi = params.get("global_enabled"), params.get("project_enabled")
    ih = params.get("interval_hours")
    return await backend.memory_maintain_save(
        global_enabled=None if gi is None else bool(gi),
        project_enabled=None if pi is None else bool(pi),
        interval_hours=None if ih is None else int(ih),
    )


async def _h_memory_maintain_now(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_maintain_now()


async def _h_memory_digest_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 归档自动记忆总闸：只切一个布尔开关，不写敏感配置，远程可调
    return await backend.memory_digest_save(bool(params.get("enabled", True)))


async def _h_memory_candidates(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 轮次沉淀的待审候选：条目带上下文摘录与会话归属（对话内容的枚举面），
    # 与 memory.get 同一姿态——本机专属
    return await backend.memory_candidates()


async def _h_memory_candidate_adopt(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_candidate_adopt(str(params.get("candidate_id", "")))


async def _h_memory_candidate_ignore(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_candidate_ignore(str(params.get("candidate_id", "")))


async def _h_memory_distill_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 轮次沉淀总闸：只切一个布尔开关（引擎自有状态文件），与 memory.digest_save
    # 同一姿态，远程可调
    return await backend.memory_distill_save(bool(params.get("enabled", False)))


async def _h_map_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 记忆地图载荷：跨项目查看是枚举面（标题/摘要/文件路径），只给本机
    return await backend.map_get(params, local=local)


async def _h_map_generate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.map_generate(params, local=local)


async def _h_map_save_config(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.map_save_config(params)


async def _h_advanced_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.advanced_settings()


async def _h_advanced_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_advanced_settings(params)


async def _h_retention_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 数据保留策略回读：各类保留天数与目录占用（只读度量面，不含敏感内容）
    return await backend.retention_status()


async def _h_retention_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.retention_save(params)


async def _h_retention_sweep(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 立即清理删的是数据目录已知目录里的过期文件，属本机维护动作
    return await backend.retention_sweep_now()


async def _h_storage_usage(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 存储占用度量（数据库大小/表行数/备份与日志体量）：只读，不含路径与内容
    return await backend.storage_usage()


async def _h_storage_cleanup(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 表级清理 + VACUUM：删行且可能长时间锁库，仅本机
    return await backend.storage_cleanup(params)


async def _h_archive_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 归档管理回读：开关/保留天数/归档总数（只读度量，不含会话内容）
    return await backend.archive_status()


async def _h_archive_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.archive_save(params)


async def _h_archive_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 跨全部项目的归档列表（含项目名）：设置 · 归档管理的本机管理视图
    return await backend.archive_manage_list()


async def _h_archive_restore(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    raw = params.get("ids")
    if not isinstance(raw, list):
        raise RuntimeError("缺少要恢复的会话清单（ids）")
    return await backend.archive_restore_batch([str(i) for i in raw])


async def _h_archive_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # ids 缺省 = 全部已归档（「全部删除」）；删除走 delete_session 完整清理链
    raw = params.get("ids")
    if raw is not None and not isinstance(raw, list):
        raise RuntimeError("ids 需要一个会话 id 数组")
    ids = None if raw is None else [str(i) for i in raw]
    return await backend.archive_delete_batch(ids)


async def _h_browser_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 浏览器控制回读：两个开关 + 内置浏览器 profile 的占用（只读度量，不含内容）
    return backend.browser_status()


async def _h_browser_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_browser_settings(params)


async def _h_browser_clear(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # mode=cache 只删缓存；mode=all 另登记「下次启动清空」（使用中删不掉 Cookie）
    return await backend.clear_browser_data(str(params.get("mode", "cache")))


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "ui.get": _WsMethod(_h_ui_get),
    "trust.status": _WsMethod(_h_trust_status),
    "trust.list": _WsMethod(_h_trust_list, local_only=True, local_error="信任清单只能在本机界面上查看"),
    "trust.revoke_path": _WsMethod(_h_trust_revoke_path),
    "trust.grant": _WsMethod(_h_trust_grant, local_only=True, local_error="工作区信任只能在本机界面上确认"),
    "trust.revoke": _WsMethod(_h_trust_revoke),
    "ui.save": _WsMethod(_h_ui_save),
    "memory.get": _WsMethod(_h_memory_get, local_only=True),
    "memory.save": _WsMethod(_h_memory_save, local_only=True),
    "memory.maintain_save": _WsMethod(_h_memory_maintain_save),
    "memory.maintain_now": _WsMethod(_h_memory_maintain_now),
    "memory.digest_save": _WsMethod(_h_memory_digest_save),
    # 轮次记忆沉淀（记忆二期）：候选条目带对话摘录与会话归属，本机专属；
    # 开关只切布尔，与 memory.digest_save 同姿态
    "memory.candidates": _WsMethod(
        _h_memory_candidates, local_only=True,
        local_error="记忆候选列表只能在本机界面上查看",
    ),
    "memory.candidate_adopt": _WsMethod(
        _h_memory_candidate_adopt, local_only=True,
        local_error="记忆候选的采纳与忽略只能在本机界面上操作",
    ),
    "memory.candidate_ignore": _WsMethod(
        _h_memory_candidate_ignore, local_only=True,
        local_error="记忆候选的采纳与忽略只能在本机界面上操作",
    ),
    "memory.distill_save": _WsMethod(_h_memory_distill_save),
    "map.get": _WsMethod(_h_map_get),
    "map.generate": _WsMethod(_h_map_generate),
    "map.save_config": _WsMethod(_h_map_save_config),
    "advanced.get": _WsMethod(_h_advanced_get),
    "advanced.save": _WsMethod(_h_advanced_save, local_only=True),
    # 数据保留策略（只进不出目录的治理）：状态各端可看；改保留天数与立即清理
    # 是删文件的动作面，与 advanced.save 同姿态仅本机
    "retention.status": _WsMethod(_h_retention_status),
    "retention.save": _WsMethod(
        _h_retention_save, local_only=True,
        local_error="数据保留设置只能在桌面端本机修改",
    ),
    "retention.sweep": _WsMethod(
        _h_retention_sweep, local_only=True,
        local_error="立即清理只能在桌面端本机执行",
    ),
    "storage.usage": _WsMethod(_h_storage_usage),
    "storage.cleanup": _WsMethod(
        _h_storage_cleanup, local_only=True,
        local_error="存储清理只能在桌面端本机执行",
    ),
    # 归档管理（设置 · 归档管理）：状态回读只读放行各端；改配置、跨项目
    # 列表与批量恢复/删除是本机管理动作（列表含其他项目的会话标题，
    # 远程不放宽；删除走 delete_session 完整清理链）
    "archive.status": _WsMethod(_h_archive_status),
    "archive.save": _WsMethod(
        _h_archive_save, local_only=True,
        local_error="归档设置只能在桌面端本机修改",
    ),
    "archive.list": _WsMethod(
        _h_archive_list, local_only=True,
        local_error="归档管理只能在本机界面上查看",
    ),
    "archive.restore": _WsMethod(
        _h_archive_restore, local_only=True,
        local_error="恢复已归档会话只能在桌面端本机操作",
    ),
    "archive.delete": _WsMethod(
        _h_archive_delete, local_only=True,
        local_error="删除已归档会话只能在桌面端本机操作",
    ),
    # 浏览器控制（设置 · 浏览器控制）：状态回读只读放行各端；改开关与清数据是
    # 本机管理动作（清数据会删本机浏览器 profile，远程不放宽）
    "browser.status": _WsMethod(_h_browser_status),
    "browser.save": _WsMethod(
        _h_browser_save, local_only=True,
        local_error="浏览器控制设置只能在桌面端本机修改",
    ),
    "browser.clear": _WsMethod(
        _h_browser_clear, local_only=True,
        local_error="清除浏览器数据只能在本机界面上操作",
    ),
}
