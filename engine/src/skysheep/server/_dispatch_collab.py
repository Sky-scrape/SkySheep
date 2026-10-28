"""WS handler（协作域）：roundtable / adversarial / team / teamcfg。
# 自 app.py 逐字迁入，行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ._dispatch_common import _conn_session, _WsMethod
from .backend import ServerBackend


async def _h_roundtable_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.roundtable_detail()


async def _h_roundtable_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.roundtable_save(params)


# 对抗：设置页配置卡（仿 roundtable.get/save：读写均各端可用，改动只影响
# 之后发起的对抗轮）；对抗轮本体走 chat.send 的 adversarial 标志。
async def _h_adversarial_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.adversarial_detail()


async def _h_adversarial_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.adversarial_save(params)


# 团队：工单板只由用户经这些方法触达，聊天仍走 chat.send（会话级活动团队
# 会把消息吸进频道；AI 总管模式经 director_mode/director 建队并自动闭环）。
# 参数错误（无活动团队 / 指派对象不在名册 / 状态机不许可 / redo 超限）按
# TeamError 原文回给调用方。
def _team_scoped_params(backend: ServerBackend, params: dict, local: bool) -> dict:
    """非本机连接把 session_id 收敛到其绑定的活动会话（沿 runs/tasks 既有口径）。

    团队快照、工单板、收队/交付/接管与频道回放只对「该连接正在交互的会话」
    生效——team.* 的归属校验不能信任调用方断言的 sid（sid 可经 session.list
    枚举）：断言他人 sid 读他人团队快照与全量频道文本、加单/改态他人工单板、
    越权交付乃至收队他人团队，在入口即被收敛拒绝。

    作用域取连接级上下文（_conn_session），不读全局 backend.session——后者
    会被另一个连接或本机窗口切走，收敛对象随全局指针漂移（安全审查 M03）；
    backend.session 为 None 时也不能放行调用方断言的 sid（旧实现因此与
    docstring 口径相反）。未绑定会话的远程连接收敛为空串 fail closed：
    team.get 回空态、写操作在 _team_for 报「没有进行中的团队」。返回新 dict，
    不改调用方的参数对象；本机调用不受影响（前端显式传 sid 的既有用法照旧）。
    """
    if local:
        return params
    return {**params, "session_id": _conn_session() or ""}


async def _h_team_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.team_get(_team_scoped_params(backend, params, local))


async def _h_team_task_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_task_add(_team_scoped_params(backend, params, local))


async def _h_adversarial_to_team_tasks(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.adversarial_to_team_tasks(_team_scoped_params(backend, params, local))


async def _h_team_task_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_task_update(_team_scoped_params(backend, params, local))


async def _h_team_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_stop(_team_scoped_params(backend, params, local))


async def _h_team_deliver(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_deliver(_team_scoped_params(backend, params, local))


async def _h_team_takeover(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_takeover(_team_scoped_params(backend, params, local))


# 三期：频道回放（收队/重启后按 meta 里的 team_id 拉全量，归属校验在 backend）、
# 建队模板（全局册：读各端可看，写仅本机——与子代理定义同姿态）、设置页配置卡
# （仿 roundtable.get/save：读写均各端可用，改动只影响之后新建的团队）。
async def _h_team_log(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_log(_team_scoped_params(backend, params, local))


# 团队室聚合探针（团队室页签专用只读方法）：team.get 快照 + latest_seq + stats
# 一次带回；非本机连接的 session_id 照走 _team_scoped_params 收敛（与 team.get/
# team.log 同一安全口径，只读得到自己会话的团队）。
async def _h_team_room(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_room(_team_scoped_params(backend, params, local))


async def _h_team_template_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.team_template_list()


async def _h_team_template_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_template_save(params)


async def _h_team_template_remove(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_template_remove(params)


# 常驻团队（T3.2）：设为常驻 / 解除常驻。写全局册（team_resident.json），
# 与模板册写面同姿态仅本机（local_only）；作用对象是「该连接正在交互的
# 会话」的活动团队，非本机连接照走 _team_scoped_params 收敛。
async def _h_team_resident_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_resident_set(_team_scoped_params(backend, params, local))


async def _h_team_resident_clear(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_resident_clear(_team_scoped_params(backend, params, local))


# 常驻登记状态探询（只读）：按 team_id 查全局册，终态团队卡凭它决定是否亮
# 「解除常驻」。team_id 无会话归属面可收敛、读的是本机全局登记——不给远程
# 客户端枚举本机常驻登记的入口，读面也仅本机。
async def _h_team_resident_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.team_resident_get(params)


async def _h_teamcfg_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.team_config_detail()


async def _h_teamcfg_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_config_save(params)


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "roundtable.get": _WsMethod(_h_roundtable_get),
    "roundtable.save": _WsMethod(_h_roundtable_save),
    "adversarial.get": _WsMethod(_h_adversarial_get),
    "adversarial.save": _WsMethod(_h_adversarial_save),
    # 团队：team.get 供团队卡与刷新；工单板只由用户改；交付（用户确认的正常
    # 终态）、收队（立即终止，含正在跑的成员轮）与接管（AI 总管 → 用户总管，
    # 二期）。聊天入口仍是 chat.send（team 标志；AI 总管经 director_mode/director）。
    # 非 local 调用的 session_id 一律收敛到该连接绑定的活动会话（_team_scoped_params，
    # 断言他人 sid 读写/收队他人团队在入口即拒）。三期另开三面：team.log 回放
    # 全量频道消息（读，一致性校验在 backend）；team.template_* 建队模板（读各
    # 端可看、写仅本机，与子代理定义同姿态）；teamcfg.get/save 设置页配置卡
    # （仿 roundtable.get/save）
    "team.get": _WsMethod(_h_team_get),
    "team.task_add": _WsMethod(_h_team_task_add),
    "adversarial.to_team_tasks": _WsMethod(_h_adversarial_to_team_tasks),
    "team.task_update": _WsMethod(_h_team_task_update),
    "team.stop": _WsMethod(_h_team_stop),
    "team.deliver": _WsMethod(_h_team_deliver),
    "team.takeover": _WsMethod(_h_team_takeover),
    "team.log": _WsMethod(_h_team_log),
    "team.room": _WsMethod(_h_team_room),
    "team.template_list": _WsMethod(_h_team_template_list),
    "team.template_save": _WsMethod(_h_team_template_save, local_only=True),
    "team.template_remove": _WsMethod(_h_team_template_remove, local_only=True),
    # 常驻团队（T3.2）：写全局册仅本机（与模板册写面同姿态）；对象收敛同 team.get；
    # resident_get 按 team_id 的只读探询同样仅本机（全局登记无会话归属面可收敛）
    "team.resident_set": _WsMethod(_h_team_resident_set, local_only=True),
    "team.resident_clear": _WsMethod(_h_team_resident_clear, local_only=True),
    "team.resident_get": _WsMethod(_h_team_resident_get, local_only=True),
    "teamcfg.get": _WsMethod(_h_teamcfg_get),
    "teamcfg.save": _WsMethod(_h_teamcfg_save),
}
