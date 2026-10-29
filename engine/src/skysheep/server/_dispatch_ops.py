"""WS handler（本机操作与外联域）：relay / hooks / app / lan / remote /
demo / websearch / imagegen / speech / settings / data / channel。
# 自 app.py 逐字迁入，行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ._dispatch_common import _WsMethod
from .backend import ServerBackend


async def _h_relaycfg_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.relay_settings()

async def _h_relaycfg_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_relay_settings(params)


async def _h_relay_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_list(params)


async def _h_relay_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_get(params)


async def _h_relay_start(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_start(params)


async def _h_relay_approve(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_approve(params)


async def _h_relay_pause(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_pause(params)


async def _h_relay_resume(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_resume(params)


async def _h_relay_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_stop(params)


async def _h_relay_dual_open(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_dual_open(params)


async def _h_relay_dual_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_dual_save(params)


async def _h_relay_dual_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_dual_get(params)


async def _h_relay_dual_run(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_dual_run(params)


async def _h_relay_dual_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.relay_dual_stop(params)


async def _h_hooks_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    result = backend.hooks_settings()
    if not local:
        # 审查 L06（M7/_p 同口径）：钩子配置文件的本机绝对路径不外泄；规则命令
        # 是用户自己配的功能面照回，远程 Hooks 面板仍可读
        result["config_path"] = ""
    return result


async def _h_hooks_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_hooks_settings(params)


async def _h_hooks_test(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 测试会真实执行命令，与 hooks.save 同一信任模型：本机专属
    return await backend.test_hook(params)


async def _h_app_notify(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.notify(params)


async def _h_app_apply_theme(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.apply_theme(params)


async def _h_app_check_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.check_update()


async def _h_app_install_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_update()


async def _h_app_apply_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.apply_update()


async def _h_app_dismiss_update_failure(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 「上次更新未完成」提示的用户确认：只清 marker，不影响当前运行版本
    return await backend.dismiss_update_failure()


async def _h_app_restart(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 重启等于在本机拉起新进程：LOCAL_ONLY_METHODS 已把远程拦下
    return await backend.app_restart()


async def _h_app_open_path(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.open_path(str(params.get("kind", "")))


async def _h_app_open_external(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.open_external(str(params.get("target", "")), local=local)


async def _h_app_export_diagnostics(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.export_diagnostics()


async def _h_lan_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 令牌就是远程访问凭据本身：只给本机界面（安全审查 A9）
    return await backend.lan_status(include_token=local)


async def _h_lan_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.lan_enable(params)


async def _h_lan_disable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.lan_disable()


async def _h_lan_connections(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 在线连接概览（ip/来源/时间/UA）：只读，不含 emit 引用与任何令牌值
    return {"connections": backend.connections_summary()}


async def _h_lan_kick(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 踢远程连接下线：仅本机可操作（踢的是别人，不能远程互相踢）
    return await backend.lan_kick(params)


async def _h_lan_rotate_token(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 换共享令牌立即生效：旧地址/二维码/cookie 全部作废（LOCAL_ONLY 已拦远程）
    return await backend.lan_rotate_token()


async def _h_lan_set_port(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 固定服务端口（PWA 的 start_url 在安装时固化 origin 含端口）：改配置类
    # 设置，LOCAL_ONLY 已拦远程
    return await backend.lan_set_port(params)


async def _h_remote_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remote_status(include_token=local)


async def _h_remote_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remote_enable(params)


async def _h_remote_disable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remote_disable()


async def _h_demo_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.demo_enable()


async def _h_websearch_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.websearch_detail()


async def _h_websearch_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.websearch_save(params)


async def _h_imagegen_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.imagegen_detail()


async def _h_imagegen_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.imagegen_save(params)


async def _h_speech_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.speech_detail()


async def _h_speech_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.speech_save(params)


async def _h_speech_transcribe(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 录音转文字：只读型能力（把音频交给已配置的服务），不弹确认；
    # 但它会向外发请求，局域网远端不给用（本地终端场景）
    return await backend.speech_transcribe(
        str(params.get("audio", "")), str(params.get("mime", ""))
    )


async def _h_settings_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.settings_export()


async def _h_settings_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.settings_import(params)


async def _h_settings_preview_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.settings_preview_import(params)


async def _h_data_export_full(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 完整导出：含会话库快照与技能正文，落本机数据目录，仅本机可触发
    return await backend.export_full_data(params)


async def _h_data_preview_import_full(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 完整导入前预览：解析导出包（含会话库 quick_check 与版本校验），仅本机
    return await backend.preview_import_full(params)


async def _h_data_import_full(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 完整导入：会话库 + 配置 + 技能整体恢复，失败自动回滚，仅本机可触发
    return await backend.import_full_data(params)


async def _h_channel_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_status()


async def _h_channel_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_save(params)


async def _h_channel_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_enable(params)


async def _h_channel_disable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_disable(params)


async def _h_channel_set_timeout(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_set_timeout(params)


async def _h_channel_test(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_test(params)


async def _h_channel_weixin_login_start(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_weixin_login_start()


async def _h_channel_weixin_login_poll(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_weixin_login_poll(params)


async def _h_channel_weixin_logout(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_weixin_logout()


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "relaycfg.get": _WsMethod(_h_relaycfg_get, local_only=True),
    "relaycfg.save": _WsMethod(_h_relaycfg_save, local_only=True),
    # 接力运行期（无工具串行 A/B）：本机 RPC；不创建会话、不执行工具、
    # 不启动 Agent。远程连接不能借 task_id 读取或推进其他项目的任务。
    "relay.list": _WsMethod(_h_relay_list, local_only=True),
    "relay.get": _WsMethod(_h_relay_get, local_only=True),
    "relay.start": _WsMethod(_h_relay_start, local_only=True),
    "relay.approve": _WsMethod(_h_relay_approve, local_only=True),
    "relay.pause": _WsMethod(_h_relay_pause, local_only=True),
    "relay.resume": _WsMethod(_h_relay_resume, local_only=True),
    "relay.stop": _WsMethod(_h_relay_stop, local_only=True),
    # 接力（主副屏交替）：主屏为当前会话、副屏为绑定会话，每轮走完整 chat
    # 管道（工具/权限门照常）。本机 RPC，远程不能看到或推进本机接力。
    "relay.dual_open": _WsMethod(_h_relay_dual_open, local_only=True),
    "relay.dual_save": _WsMethod(_h_relay_dual_save, local_only=True),
    "relay.dual_get": _WsMethod(_h_relay_dual_get, local_only=True),
    "relay.dual_run": _WsMethod(_h_relay_dual_run, local_only=True),
    "relay.dual_stop": _WsMethod(_h_relay_dual_stop, local_only=True),
    "hooks.get": _WsMethod(_h_hooks_get),
    "hooks.save": _WsMethod(_h_hooks_save, local_only=True),
    "hooks.test": _WsMethod(_h_hooks_test, local_only=True, local_error="钩子测试只能在本机界面上操作"),
    "app.notify": _WsMethod(_h_app_notify, local_only=True),
    # 桌面壳标题栏主题联动只应由本机界面的主题切换触发；远程界面切自己的主题
    # 不该改桌面机的窗口外观（本机体验面，与 app.notify 同族）
    "app.apply_theme": _WsMethod(
        _h_app_apply_theme, local_only=True,
        local_error="桌面主题联动只能在桌面端本机触发",
    ),
    "app.check_update": _WsMethod(_h_app_check_update),
    "app.install_update": _WsMethod(_h_app_install_update, local_only=True),
    "app.apply_update": _WsMethod(_h_app_apply_update, local_only=True),
    "app.dismiss_update_failure": _WsMethod(_h_app_dismiss_update_failure, local_only=True),
    "app.restart": _WsMethod(_h_app_restart, local_only=True),
    # 用系统文件管理器打开本机目录（support.open_folder 弹资源管理器窗口）：
    # 远程触发桌面机弹窗与 fs.open 同面（安全审查 L06），且 open_external 对远端
    # 已是「不在服务端代开」口径，这里对齐
    "app.open_path": _WsMethod(_h_app_open_path, local_only=True),
    "app.open_external": _WsMethod(_h_app_open_external),
    "app.export_diagnostics": _WsMethod(_h_app_export_diagnostics, local_only=True),
    "lan.status": _WsMethod(_h_lan_status),
    "lan.enable": _WsMethod(_h_lan_enable, local_only=True),
    "lan.disable": _WsMethod(_h_lan_disable),
    "lan.rotate_token": _WsMethod(_h_lan_rotate_token, local_only=True),
    "lan.connections": _WsMethod(_h_lan_connections),
    "lan.kick": _WsMethod(
        _h_lan_kick, local_only=True,
        local_error="断开远程连接只能在本机界面上操作",
    ),
    "lan.set_port": _WsMethod(_h_lan_set_port, local_only=True),
    "remote.status": _WsMethod(_h_remote_status),
    "remote.enable": _WsMethod(_h_remote_enable, local_only=True),
    "remote.disable": _WsMethod(_h_remote_disable),
    "demo.enable": _WsMethod(_h_demo_enable, local_only=True),
    "websearch.get": _WsMethod(_h_websearch_get),
    "websearch.save": _WsMethod(_h_websearch_save, local_only=True),
    "imagegen.get": _WsMethod(_h_imagegen_get),
    "imagegen.save": _WsMethod(_h_imagegen_save, local_only=True),
    "speech.get": _WsMethod(_h_speech_get),
    "speech.save": _WsMethod(_h_speech_save, local_only=True),
    "speech.transcribe": _WsMethod(
        _h_speech_transcribe, local_only=True,
        local_error="语音输入只能在本机界面上使用",
    ),
    "settings.export": _WsMethod(_h_settings_export, local_only=True),
    "settings.import": _WsMethod(_h_settings_import, local_only=True),
    "settings.preview_import": _WsMethod(_h_settings_preview_import, local_only=True),
    "data.export_full": _WsMethod(_h_data_export_full, local_only=True),
    "data.preview_import_full": _WsMethod(_h_data_preview_import_full, local_only=True),
    "data.import_full": _WsMethod(_h_data_import_full, local_only=True),
    # 聊天软件渠道：写配置/启停属降低防护的操作，与开局域网同一姿态——仅本机可改。
    # 远端（局域网/ tailnet）能看状态，但不能改，否则一个被扫码链接挟持的手机
    # 就能把 Agent 接给任意聊天账号。
    "channel.status": _WsMethod(_h_channel_status),
    "channel.save": _WsMethod(
        _h_channel_save, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.enable": _WsMethod(
        _h_channel_enable, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.disable": _WsMethod(
        _h_channel_disable, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.set_timeout": _WsMethod(
        _h_channel_set_timeout, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.test": _WsMethod(
        _h_channel_test, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    # 微信扫码登录：生成二维码 / 轮询确认 / 退出登录。扫码得到的是能操控 Agent 的凭据，
    # 与启用渠道同一敏感度，因此同样只允许本机操作。
    "channel.weixin_login_start": _WsMethod(
        _h_channel_weixin_login_start, local_only=True,
        local_error="微信扫码登录只能在桌面端本机完成",
    ),
    "channel.weixin_login_poll": _WsMethod(
        _h_channel_weixin_login_poll, local_only=True,
        local_error="微信扫码登录只能在桌面端本机完成",
    ),
    "channel.weixin_logout": _WsMethod(
        _h_channel_weixin_logout, local_only=True,
        local_error="微信登录状态只能在桌面端本机修改",
    ),
}
