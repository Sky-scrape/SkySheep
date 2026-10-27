"""WS handler（自动化域）：schedule / cron / pipeline / automation /
run_center / factory。自 app.py 逐字迁入，行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ._dispatch_common import _WsMethod
from .backend import ServerBackend


async def _h_schedule_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.store.list_schedules(
        include_done=bool(params.get("include_done", False))
    )


async def _h_schedule_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.schedule_add(params)


async def _h_schedule_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.schedule_update(params)


async def _h_schedule_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.schedule_delete(int(params.get("id", 0)))


async def _h_schedule_due(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"schedules": await backend.store.due_schedules()}


async def _h_cron_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_list()


async def _h_cron_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_add(params)


async def _h_cron_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_update(params)


async def _h_cron_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_delete(params)


async def _h_cron_run_now(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_run_now(params)


async def _h_cron_team_check(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 定时团队任务的模板即时校验（表单期预检；落库时 cron.add/update 仍二次校验）
    return await backend.cron_team_check(params)


async def _h_cron_schtask_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_schtask_export(params)


async def _h_cron_schtask_remove(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_schtask_remove(params)


async def _h_cron_schtask_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_schtask_status(params)


async def _h_pipeline_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_list()


async def _h_pipeline_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_get(params)


async def _h_pipeline_create(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_create(params)


async def _h_pipeline_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_update(params)


async def _h_pipeline_start(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_start(params)


async def _h_pipeline_cancel(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_cancel(params)


async def _h_pipeline_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_delete(params)


async def _h_pipeline_node_rerun(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_node_rerun(params)


async def _h_pipeline_attach(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_attach(params)


async def _h_pipeline_import_cron(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_import_cron(params)


async def _h_pipeline_add_session(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_add_session(params)


async def _h_pipeline_add_task(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_add_task(params)


async def _h_pipeline_duplicate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_duplicate(params)


async def _h_pipeline_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_export(params)


async def _h_pipeline_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_import(params)


async def _h_automation_report_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 每日运行日报的设置回读：只有开关 / 时刻 / 上次已发日期，不含敏感面
    return await backend.daily_report_status()


async def _h_automation_report_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.daily_report_save(params)


async def _h_run_center_summary(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 今日运行总览（只读聚合）：明细已按当前项目过滤（B14 同款），远端与本地同视图
    return await backend.run_center_summary()


# ---- 工厂（跨项目自动化观战）：全部本机专属，跨项目枚举面不对远程开放 ----

async def _h_factory_state(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.factory_state()


async def _h_factory_runs(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.factory_runs(params)


async def _h_factory_live(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 只读 RunCenter 内存（在跑的对话轮 / 子代理），不查库；工厂轮询用
    return await backend.factory_live()


async def _h_factory_weather(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 仅当 [factory] weather 开启且填了城市时出网；30 分钟缓存
    return await backend.factory_weather(params)


async def _h_factory_run_now(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 派工复用 _run_cron_task(force=True)：权限门/白名单/工作目录仍按目标项目
    return await backend.factory_run_now(params)


async def _h_factory_toggle(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.factory_toggle(params)


async def _h_factory_cancel(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 叫停只取消本轮运行：不放松权限、不改启用状态与排期
    return await backend.factory_cancel(params)


async def _h_factory_ack(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 「需留意」已处理标记：只写一个时间戳，不删运行历史
    return await backend.factory_ack(params)


async def _h_factory_line_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 写入的是普通 cron_tasks 行（提示词是纯文本，永不作为命令执行）
    return await backend.factory_line_add(params)


async def _h_factory_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.factory_save(params)


async def _h_factory_run_mark(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 人工判定「这轮算不算做成」：只改运行行的 outcome，不删历史、不改状态
    return await backend.factory_run_mark(params)


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "schedule.list": _WsMethod(_h_schedule_list),
    "schedule.add": _WsMethod(_h_schedule_add),
    "schedule.update": _WsMethod(_h_schedule_update),
    "schedule.delete": _WsMethod(_h_schedule_delete),
    "schedule.due": _WsMethod(_h_schedule_due),
    "cron.list": _WsMethod(_h_cron_list),
    "cron.add": _WsMethod(_h_cron_add, local_only=True),
    "cron.update": _WsMethod(_h_cron_update, local_only=True),
    "cron.delete": _WsMethod(_h_cron_delete, local_only=True),
    "cron.run_now": _WsMethod(_h_cron_run_now, local_only=True),
    "cron.team_check": _WsMethod(_h_cron_team_check, local_only=True),
    "cron.schtask_export": _WsMethod(_h_cron_schtask_export, local_only=True),
    "cron.schtask_remove": _WsMethod(_h_cron_schtask_remove, local_only=True),
    "cron.schtask_status": _WsMethod(_h_cron_schtask_status),
    "pipeline.list": _WsMethod(_h_pipeline_list),
    "pipeline.get": _WsMethod(_h_pipeline_get),
    "pipeline.create": _WsMethod(_h_pipeline_create, local_only=True),
    "pipeline.update": _WsMethod(_h_pipeline_update, local_only=True),
    "pipeline.start": _WsMethod(_h_pipeline_start, local_only=True),
    "pipeline.cancel": _WsMethod(_h_pipeline_cancel, local_only=True),
    "pipeline.delete": _WsMethod(_h_pipeline_delete, local_only=True),
    "pipeline.node_rerun": _WsMethod(_h_pipeline_node_rerun, local_only=True),
    "pipeline.attach": _WsMethod(_h_pipeline_attach, local_only=True),
    "pipeline.import_cron": _WsMethod(_h_pipeline_import_cron, local_only=True),
    "pipeline.add_session": _WsMethod(_h_pipeline_add_session, local_only=True),
    "pipeline.add_task": _WsMethod(_h_pipeline_add_task, local_only=True),
    "pipeline.duplicate": _WsMethod(_h_pipeline_duplicate, local_only=True),
    "pipeline.export": _WsMethod(_h_pipeline_export),
    "pipeline.import": _WsMethod(_h_pipeline_import, local_only=True),
    # 每日运行日报（无人值守三期）：状态只读各端可看；开启属无人值守推送面，
    # 与 cron.add 同一姿态仅本机
    "automation.daily_report_status": _WsMethod(_h_automation_report_status),
    "automation.daily_report_save": _WsMethod(
        _h_automation_report_save, local_only=True,
        local_error="运行日报的设置只能在桌面端本机修改",
    ),
    # 今日运行总览：只读聚合，明细已按当前项目过滤，各端同视图可看
    "automation.run_center_summary": _WsMethod(_h_run_center_summary),
    # 工厂（跨项目自动化观战）：十一个方法全部本机专属——状态与运行历史是跨项目
    # 枚举面，派工/启停/叫停/新建产线是无人值守执行面，天气会按本机配置出网，
    # 远程 / 手机连接一律拒绝，前端打开工厂只显示「工厂仅在本机可用」。
    "factory.state": _WsMethod(
        _h_factory_state, local_only=True,
        local_error="工厂仅在本机可用",
    ),
    "factory.runs": _WsMethod(
        _h_factory_runs, local_only=True,
        local_error="工厂运行历史只能在本机查看",
    ),
    "factory.live": _WsMethod(
        _h_factory_live, local_only=True,
        local_error="工厂实时状态只能在本机查看",
    ),
    "factory.weather": _WsMethod(
        _h_factory_weather, local_only=True,
        local_error="工厂天气只能在本机查看",
    ),
    "factory.run_now": _WsMethod(
        _h_factory_run_now, local_only=True,
        local_error="工厂派工只能在桌面端本机执行",
    ),
    "factory.toggle": _WsMethod(
        _h_factory_toggle, local_only=True,
        local_error="产线启停只能在桌面端本机操作",
    ),
    "factory.cancel": _WsMethod(
        _h_factory_cancel, local_only=True,
        local_error="叫停产线只能在桌面端本机操作",
    ),
    "factory.ack": _WsMethod(
        _h_factory_ack, local_only=True,
        local_error="工厂的需留意标记只能在本机处理",
    ),
    "factory.line_add": _WsMethod(
        _h_factory_line_add, local_only=True,
        local_error="新建产线只能在桌面端本机操作",
    ),
    "factory.save": _WsMethod(
        _h_factory_save, local_only=True,
        local_error="工厂设置只能在桌面端本机修改",
    ),
    "factory.run_mark": _WsMethod(
        _h_factory_run_mark, local_only=True,
        local_error="运行判定只能在本机修改",
    ),
}
