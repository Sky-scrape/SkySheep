# WS 方法参考

> 本文件由 engine/tools/gen_ws_reference.py 自动生成，改协议后重跑 `uv run python tools/gen_ws_reference.py`，勿手改。

- 生成时间：2026-10-30 10:00:42
- 方法总数：318（本机专属 local_only 168 个）
- 说明：dispatch handler 的 docstring 首行；无 docstring 记「—」。
- local_only：是 = 远程客户端（局域网 / Tailscale 持令牌）不可调用，仅桌面端本机可执行。

## 轮次与会话运行域（`_dispatch_chat.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| boot |  | — |
| chat.send |  | — |
| chat.status |  | — |
| chat.compact | 是 | — |
| chat.aux |  | — |
| permission.respond |  | — |
| permission.mode |  | — |
| permission.set_mode |  | — |
| stop |  | — |
| tasks.list |  | — |
| tasks.get |  | — |
| tasks.cancel_all |  | — |
| tasks.cancel |  | — |
| aux.model.get |  | — |
| aux.model.set | 是 | — |
| aux.clear |  | — |
| model.list |  | — |
| model.reasoning |  | — |
| model.set_reasoning | 是 | — |
| model.switch | 是 | — |
| default_model.get |  | — |
| default_model.set | 是 | — |
| runs.list |  | — |
| runs.get |  | — |
| runs.cancel |  | — |
| runs.retry |  | 失败/中断运行的一键重发/重跑：把原文再发一次（同会话）。 |
| model.test | 是 | — |
| model.probe_capabilities | 是 | — |
| model.providers_health |  | — |
| permission.queue |  | 当前未决权限请求队列（本机跨会话聚合，运行中心「等待确认」分区用）。 |
| config.probe_models | 是 | — |
| config.probe_context | 是 | — |

## 会话库域（`_dispatch_session.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| session.search |  | — |
| session.list |  | — |
| session.new |  | — |
| session.new_task |  | — |
| session.truncate |  | — |
| session.fork |  | — |
| session.activate |  | — |
| session.resume |  | — |
| session.image |  | — |
| session.delete |  | — |
| session.project_path | 是 | — |
| session.reveal | 是 | — |
| session.peek_messages |  | — |
| messages.earlier |  | — |
| session.model_switch | 是 | — |
| session.model_get | 是 | — |
| session.accept_get | 是 | — |
| session.accept_set | 是 | — |
| session.reasoning_get | 是 | — |
| session.reasoning_set | 是 | — |
| session.export |  | — |
| session.cleanup_empty |  | — |
| session.rename |  | — |
| session.pin |  | — |
| session.archive |  | — |
| session.list_archived |  | — |
| session.move |  | — |
| session.tags |  | — |
| session.tags_list |  | — |
| session.backups |  | — |
| session.restore_backup | 是 | — |
| session.create_backup | 是 | — |
| session.delete_backup | 是 | — |
| checkpoint.list |  | — |
| checkpoint.restore |  | — |
| checkpoint.diff |  | — |
| diagnostics.turn_breakdown |  | — |
| term.spawn | 是 | — |
| term.input | 是 | — |
| term.resize | 是 | — |
| term.stop |  | — |
| term.close | 是 | — |
| usage.stats |  | — |
| usage.collab_report |  | — |
| usage.pricing_set | 是 | — |

## 项目与文件域（`_dispatch_files.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| project.list |  | — |
| project.switch | 是 | — |
| project.delete | 是 | — |
| project.task_list |  | — |
| project.task_add |  | — |
| project.task_update |  | — |
| project.task_delete |  | — |
| project.instructions |  | — |
| project.save_instructions | 是 | — |
| fs.read |  | — |
| fs.write |  | — |
| fs.files |  | — |
| fs.open | 是 | — |
| snippets.list |  | — |
| snippets.add |  | — |
| snippets.update |  | — |
| snippets.set_enabled |  | — |
| snippets.polish |  | — |
| snippets.delete |  | — |
| snippets.used |  | — |
| snippets.reorder |  | — |
| snippets.restore_builtin |  | — |
| snippets.export |  | — |
| snippets.import |  | — |

## 自动化域（`_dispatch_automation.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| schedule.list |  | — |
| schedule.add |  | — |
| schedule.update |  | — |
| schedule.delete |  | — |
| schedule.due |  | — |
| cron.list |  | — |
| cron.add | 是 | — |
| cron.update | 是 | — |
| cron.delete | 是 | — |
| cron.run_now | 是 | — |
| cron.team_check | 是 | — |
| spectate.snapshot | 是 | — |
| spectate.send | 是 | — |
| cron.schtask_export | 是 | — |
| cron.schtask_remove | 是 | — |
| cron.schtask_status |  | — |
| pipeline.list |  | — |
| pipeline.get |  | — |
| pipeline.create | 是 | — |
| pipeline.update | 是 | — |
| pipeline.start | 是 | — |
| pipeline.cancel | 是 | — |
| pipeline.delete | 是 | — |
| pipeline.node_rerun | 是 | — |
| pipeline.attach | 是 | — |
| pipeline.import_cron | 是 | — |
| pipeline.add_session | 是 | — |
| pipeline.add_task | 是 | — |
| pipeline.duplicate | 是 | — |
| pipeline.export |  | — |
| pipeline.import | 是 | — |
| automation.daily_report_status |  | — |
| automation.daily_report_save | 是 | — |
| automation.run_center_summary |  | — |
| factory.state | 是 | — |
| factory.runs | 是 | — |
| factory.live | 是 | — |
| factory.weather | 是 | — |
| factory.run_now | 是 | — |
| factory.toggle | 是 | — |
| factory.cancel | 是 | — |
| factory.ack | 是 | — |
| factory.line_add | 是 | — |
| factory.save | 是 | — |
| factory.run_mark | 是 | — |

## 偏好与治理域（`_dispatch_prefs.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| ui.get |  | — |
| trust.status |  | — |
| trust.list | 是 | — |
| trust.revoke_path |  | — |
| trust.grant | 是 | — |
| trust.revoke |  | — |
| ui.save |  | — |
| memory.get | 是 | — |
| memory.save | 是 | — |
| memory.maintain_save |  | — |
| memory.maintain_now |  | — |
| memory.digest_save |  | — |
| memory.candidates | 是 | — |
| memory.candidate_adopt | 是 | — |
| memory.candidate_ignore | 是 | — |
| memory.distill_save |  | — |
| map.get |  | — |
| map.generate |  | — |
| map.save_config |  | — |
| advanced.get |  | — |
| advanced.save | 是 | — |
| retention.status |  | — |
| retention.save | 是 | — |
| retention.sweep | 是 | — |
| storage.usage |  | — |
| storage.cleanup | 是 | — |
| archive.status |  | — |
| archive.save | 是 | — |
| archive.list | 是 | — |
| archive.restore | 是 | — |
| archive.delete | 是 | — |
| browser.status |  | — |
| browser.save | 是 | — |
| browser.clear | 是 | — |

## 本机操作与外联域（`_dispatch_ops.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| relaycfg.get | 是 | — |
| relaycfg.save | 是 | — |
| relay.list | 是 | — |
| relay.get | 是 | — |
| relay.start | 是 | — |
| relay.approve | 是 | — |
| relay.pause | 是 | — |
| relay.resume | 是 | — |
| relay.stop | 是 | — |
| relay.dual_open | 是 | — |
| relay.dual_save | 是 | — |
| relay.dual_get | 是 | — |
| relay.dual_run | 是 | — |
| relay.dual_stop | 是 | — |
| hooks.get |  | — |
| hooks.save | 是 | — |
| hooks.test | 是 | — |
| app.notify | 是 | — |
| app.apply_theme | 是 | — |
| app.check_update |  | — |
| app.install_update | 是 | — |
| app.apply_update | 是 | — |
| app.dismiss_update_failure | 是 | — |
| app.restart | 是 | — |
| app.open_path | 是 | — |
| app.open_external |  | — |
| app.export_diagnostics | 是 | — |
| lan.status |  | — |
| lan.enable | 是 | — |
| lan.disable |  | — |
| lan.rotate_token | 是 | — |
| lan.connections |  | — |
| lan.kick | 是 | — |
| lan.set_port | 是 | — |
| remote.status |  | — |
| remote.enable | 是 | — |
| remote.disable |  | — |
| demo.enable | 是 | — |
| websearch.get |  | — |
| websearch.save | 是 | — |
| imagegen.get |  | — |
| imagegen.save | 是 | — |
| speech.get |  | — |
| speech.save | 是 | — |
| speech.transcribe | 是 | — |
| settings.export | 是 | — |
| settings.import | 是 | — |
| settings.preview_import | 是 | — |
| data.export_full | 是 | — |
| data.preview_import_full | 是 | — |
| data.import_full | 是 | — |
| migration.export | 是 | 跨机迁移整包导出：会话库快照+技能+ui.json+脱敏 config 打包 base64 回传。 |
| migration.import | 是 | 跨机迁移整包导入：白名单校验+先备份本机库后替换，改了库需重启生效。 |
| channel.status |  | — |
| channel.save | 是 | — |
| channel.enable | 是 | — |
| channel.disable | 是 | — |
| channel.set_timeout | 是 | — |
| channel.test | 是 | — |
| channel.weixin_login_start | 是 | — |
| channel.weixin_login_poll | 是 | — |
| channel.weixin_logout | 是 | — |

## 协作域（`_dispatch_collab.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| roundtable.get |  | — |
| roundtable.save |  | — |
| adversarial.get |  | — |
| adversarial.save |  | — |
| team.get |  | — |
| team.task_add |  | — |
| adversarial.to_team_tasks |  | — |
| adversarial.export |  | — |
| roundtable.to_team_task |  | — |
| team.task_update |  | — |
| team.stop |  | — |
| team.deliver |  | — |
| team.takeover |  | — |
| team.log |  | — |
| team.room |  | — |
| team.template_list |  | — |
| team.template_save | 是 | — |
| team.template_remove | 是 | — |
| team.resident_set | 是 | — |
| team.resident_clear | 是 | — |
| team.resident_get | 是 | — |
| teamcfg.get |  | — |
| teamcfg.save |  | — |

## 扩展生态域（`_dispatch_ext.py`）

| 方法 | local_only | 说明 |
| --- | --- | --- |
| config.add_provider_model | 是 | — |
| config.remove_provider_model | 是 | — |
| config.providers |  | — |
| config.save_provider | 是 | — |
| config.add_provider | 是 | — |
| config.delete_provider | 是 | — |
| config.restore_provider | 是 | — |
| config.set_provider_enabled | 是 | — |
| subagent.get |  | — |
| subagent.save | 是 | — |
| subagent.save_builtin | 是 | — |
| subagent.save_custom | 是 | — |
| subagent.delete_custom | 是 | — |
| whitelist.remove |  | — |
| whitelist.add | 是 | — |
| whitelist.clear |  | — |
| whitelist.check |  | — |
| whitelist.export |  | — |
| whitelist.enable |  | — |
| whitelist.import | 是 | — |
| skills.toggle | 是 | — |
| extension.audit.list |  | 扩展变更审计：最近 N 条（最近在前）。只读，远程也可查自己的可见部分。 |
| skills.scope | 是 | — |
| skills.body |  | — |
| skills.scan_local |  | — |
| skills.install | 是 | — |
| skills.preview_install | 是 | — |
| skills.delete | 是 | — |
| skills.gallery |  | — |
| skills.save_from_session |  | — |
| skills.save_draft | 是 | — |
| mods.list |  | — |
| mods.get |  | — |
| mods.get_template |  | — |
| mods.official |  | — |
| mods.install | 是 | — |
| mods.confirm_install | 是 | — |
| mods.install_official | 是 | — |
| mods.delete | 是 | — |
| mods.toggle | 是 | — |
| mods.set_enabled | 是 | — |
| mods_claude.action | 是 | Claude 兼容模式的 UI 交互回传（按钮点击 / 输入变更）。 |
| mods.save_draft | 是 | — |
| mods.test | 是 | — |
| mcp.import | 是 | — |
| mcp.save_server | 是 | — |
| mcp.delete | 是 | — |
| mcp.add_preset | 是 | — |
| mcp.set_enabled | 是 | — |
| mcp.reconnect | 是 | — |
| mcp.status |  | — |
| tools.list |  | — |
| whitelist.list |  | — |
