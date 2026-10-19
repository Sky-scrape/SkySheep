"""高级设置与系统集成：设置页高级卡、钩子设置、本地目录/诊断、首启激活。

从 backend.py 按职责注释整段搬入。方法体逐字保留，逻辑不变。
覆盖四个相邻职责区：高级设置（主循环轮数 / 上下文上限 / 压缩保留 /
目录限制 / 开机自启 / 热键）、钩子（设置页读写与测试）、本地目录与
诊断包（系统集成入口）、首启激活（演示模式）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from ...config import (
    ConfigError,
    config_path,
    load_config,
    set_advanced_settings_in_config,
    set_hooks_in_config,
    skysheep_home,
    update_provider_in_config,
)
from ...core.hooks import (
    HookRule,
    HookRunner,
    hooks_from_config,
    load_raw_config,
    recent_hook_runs,
)
from .relay import RelaySettingsMixin


class SystemMixin(RelaySettingsMixin):
    """高级设置、钩子设置与系统集成（目录/诊断/首启激活）。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- 高级设置（主循环轮数 / 上下文上限 / 压缩保留 / 目录限制 / 开机自启） ----

    def _apply_advanced_to_agents(self) -> None:
        """把改动推给所有活着的 Agent，不用重启应用。"""
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.max_iterations = self.cfg.max_iterations
            ag.context_limit_tokens = limit
            ag.compaction_keep_recent = self.cfg.compaction_keep_recent
            ag.compaction_trigger = self.cfg.compaction_trigger
            ag.compaction_auto = self.cfg.compaction_auto
            ag.restrict_to_workdir = self.cfg.restrict_to_workdir
            ag.job_containment = self.cfg.shell.job_containment
            # 沙箱级别与遏制同一条热更通道（run_command 每次执行时从工具
            # 上下文取值；Agent 未带该属性的旧构造点赋值也无害）
            ag.sandbox_level = self.cfg.shell.sandbox_level

    def _startup_status(self) -> dict:
        from ... import startup

        try:
            return startup.status()
        except Exception as e:  # noqa: BLE001 - 注册表异常不该让设置页打不开
            return {"supported": startup.is_supported(), "enabled": False,
                    "command": "", "current": "", "stale": False, "error": str(e)}

    def _hotkey_pref(self) -> str:
        """全局唤起热键偏好（校验在 desktop._parse_hotkey，注册时非法值自带回退，
        这里只读不验避免两处规则漂移）。desktop 是打包壳、服务进程里没有，
        所以独立实现读取，不 import。"""
        try:
            return str(self._read_ui_prefs().get("hotkey") or "")
        except Exception:  # noqa: BLE001
            return ""

    def hooks_settings(self) -> dict:
        """当前 config.toml 里的钩子规则（设置页 · Hooks 面板）。

        同时回传启用中的规则条数：未生效与没配置是两回事，界面要能区分；
        另带最近执行记录——钩子自身故障（非 0 非 2 退出码）按设计静默放行，
        但必须让用户在设置页看到它失效了，而不是「配了像没配一样」。
        """
        raw = load_raw_config()
        pre, post, stop = hooks_from_config(raw)
        hooks = raw.get("hooks") if isinstance(raw.get("hooks"), dict) else {}

        def _pub(key: str) -> list[dict]:
            out: list[dict] = []
            for item in hooks.get(key) or []:
                if isinstance(item, dict) and str(item.get("command", "")).strip():
                    out.append({
                        "match": str(item.get("match", "*") or "*"),
                        "command": str(item["command"]).strip(),
                        "timeout_s": float(item.get("timeout_s") or 10),
                        "enabled": bool(item.get("enabled", True)),
                    })
            return out

        return {
            "pre": _pub("pre_tool_use"),
            "post": _pub("post_tool_use"),
            "stop": _pub("stop"),
            "active_pre": len([r for r in pre if r.enabled]),
            "active_post": len([r for r in post if r.enabled]),
            "active_stop": len([r for r in stop if r.enabled]),
            "recent": recent_hook_runs(),
            "config_path": str(config_path()),
            "tool_names": sorted(t.name for t in self._build_full_registry().all()),
        }

    async def save_hooks_settings(self, params: dict) -> dict:
        """保存钩子规则并热生效（重建 HookRunner 并推给所有活动 Agent）。

        钩子命令等价于用户自己敲的命令（来自用户配置），不走权限门——但它是
        「工具调用前的最后一道人工闸门」，所以保存动作本身写进日志便于回查。
        """
        pre = params.get("pre")
        post = params.get("post")
        stop = params.get("stop")
        try:
            set_hooks_in_config(
                pre=None if pre is None else list(pre),
                post=None if post is None else list(post),
                stop=None if stop is None else list(stop),
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self._reload_hooks()
        return self.hooks_settings()

    async def test_hook(self, params: dict) -> dict:
        """钩子测试器：拿示例参数把一条钩子命令实跑一遍，回完整结果。

        与 hooks.save 同一信任模型——命令来自本机界面上的用户输入，等价于手敲，
        不经过权限门，因此同列为本机专属方法（远程客户端不可调用）。
        只执行、不落配置：测试的命令不保存，也不进「最近执行」记录。
        """
        kind = str(params.get("kind") or "pre")
        if kind not in ("pre", "post", "stop"):
            raise RuntimeError("钩子类型只能是 pre / post / stop")
        command = str(params.get("command") or "").strip()
        if not command:
            raise RuntimeError("请先填写要测试的命令")
        if "\n" in command:
            raise RuntimeError("命令不能包含换行")
        try:
            timeout_s = float(params.get("timeout_s") or 10)
        except (TypeError, ValueError):
            raise RuntimeError("超时秒数必须是数字") from None
        tool = str(params.get("tool") or "").strip() or "write_file"
        raw_input = params.get("input")
        input_dict = raw_input if isinstance(raw_input, dict) else {}
        rule = HookRule(match="*", command=command, timeout_s=timeout_s)
        runner = HookRunner(
            [rule] if kind == "pre" else [],
            [rule] if kind == "post" else [],
            working_dir=self.working_dir,
            stop_rules=[rule] if kind == "stop" else [],
        )
        return await runner.test_run(kind, rule, tool, input_dict)

    def _reload_hooks(self) -> None:
        """重新读 hooks 配置并推给基础 Agent 与所有会话 Agent（不重启即生效）。"""
        raw_cfg = load_raw_config()
        pre_rules, post_rules, stop_rules = hooks_from_config(raw_cfg)
        # 无规则时置 None：Agent 循环里的判断是「hooks is not None」，
        # 空 Runner 也会走一遍调用链，没必要
        self.hooks = HookRunner(pre_rules, post_rules, working_dir=self.working_dir,
                                stop_rules=stop_rules) \
            if (pre_rules or post_rules or stop_rules) else None
        if hasattr(self, "_base_agent") and self._base_agent is not None:
            self._base_agent.hooks = self.hooks
        for ag in self._for_each_agent():
            ag.hooks = self.hooks
        # 团队成员 Agent 的钩子同步热更（成员不在 _for_each_agent，构造时
        # 注入的 HookRunner 不跟着刷新就会对队员悄悄失效）
        for orch in self._teams.values():
            for ag in orch.agents():
                ag.hooks = self.hooks

    def advanced_settings(self) -> dict:

        return {
            "max_iterations": self.cfg.max_iterations,
            "context_limit_tokens": self.cfg.context_limit_tokens,
            "compaction_keep_recent": self.cfg.compaction_keep_recent,
            "compaction_trigger": self.cfg.compaction_trigger,
            "compaction_auto": self.cfg.compaction_auto,
            "restrict_to_workdir": self.cfg.restrict_to_workdir,
            "computer_control": self.cfg.computer_control,
            "browser_control": self.cfg.browser_control,
            "daily_token_budget": self.cfg.daily_token_budget,
            "turn_token_budget": self.cfg.turn_token_budget,
            "fallback_model": self.cfg.fallback_model,
            # 「安全与后台」的三项：定时调度总开关与命令沙箱（读取点都在各自
            # 执行路径上按当前配置取值，改完即热生效，无需重启）
            "system_schedule": self.cfg.cron.system_schedule,
            "job_containment": self.cfg.shell.job_containment,
            "sandbox_level": self.cfg.shell.sandbox_level,
            "context_limit_tokens_effective": self._context_limit(),
            "current_provider": self.provider_name,
            "current_provider_context_limit": (
                self._current_provider_cfg().context_limit if self._current_provider_cfg() else 0
            ),
            "home": str(skysheep_home()),
            "logs_dir": str(skysheep_home() / "logs"),
            "working_dir": str(self.working_dir or ""),
            "autostart": self._startup_status(),
            # 全局唤起热键（仅 Windows 桌面版生效；其它平台 supported=False）
            "hotkey": self._hotkey_pref(),
            "hotkey_supported": sys.platform == "win32",
        }

    async def save_advanced_settings(self, params: dict) -> dict:
        """保存高级设置并热生效；含开机自启开关（Windows 当前用户 Run 项）。"""
        ints: dict[str, int] = {}
        for key in ("max_iterations", "context_limit_tokens", "compaction_keep_recent"):
            if params.get(key) is not None and params.get(key) != "":
                try:
                    ints[key] = int(params[key])
                except (TypeError, ValueError):
                    raise RuntimeError(f"{key} 需要一个整数") from None
        # 压缩触发比例：前端按百分数编辑，传比例（0.5–0.98）；越界在 config 层报错
        trigger = params.get("compaction_trigger")
        if trigger is not None and trigger != "":
            try:
                trigger = float(trigger)
            except (TypeError, ValueError):
                raise RuntimeError("压缩触发比例需要一个数字") from None
        else:
            trigger = None
        # 自动压缩总开关：不传 = 不动（与其它布尔项同一姿态）
        compaction_auto = params.get("compaction_auto")
        restrict = params.get("restrict_to_workdir")
        budget = params.get("daily_token_budget")
        if budget is not None and budget != "":
            try:
                budget = int(budget)
            except (TypeError, ValueError):
                raise RuntimeError("每日 token 预算需要一个整数") from None
        else:
            budget = None
        turn_budget = params.get("turn_token_budget")
        if turn_budget is not None and turn_budget != "":
            try:
                turn_budget = int(turn_budget)
            except (TypeError, ValueError):
                raise RuntimeError("单轮 token 上限需要一个整数") from None
        else:
            turn_budget = None
        fallback = params.get("fallback_model")
        if fallback is not None:
            fallback = str(fallback).strip()
        try:
            set_advanced_settings_in_config(
                max_iterations=ints.get("max_iterations"),
                context_limit_tokens=ints.get("context_limit_tokens"),
                compaction_keep_recent=ints.get("compaction_keep_recent"),
                compaction_trigger=trigger,
                compaction_auto=None if compaction_auto is None else bool(compaction_auto),
                restrict_to_workdir=None if restrict is None else bool(restrict),
                computer_control=(
                    None if params.get("computer_control") is None
                    else bool(params.get("computer_control"))
                ),
                browser_control=(
                    None if params.get("browser_control") is None
                    else bool(params.get("browser_control"))
                ),
                daily_token_budget=budget,
                turn_token_budget=turn_budget,
                fallback_model=None if fallback is None else fallback,
                # 「安全与后台」三项：不传 = 不动（与其它布尔项同一姿态）；
                # 读取点在执行路径上按配置取值，self.cfg 重载后即热生效
                system_schedule=(
                    None if params.get("system_schedule") is None
                    else bool(params.get("system_schedule"))
                ),
                job_containment=(
                    None if params.get("job_containment") is None
                    else bool(params.get("job_containment"))
                ),
                sandbox_level=(
                    None if params.get("sandbox_level") is None
                    else str(params.get("sandbox_level"))
                ),
                use_ripgrep=(
                    None if params.get("use_ripgrep") is None
                    else bool(params.get("use_ripgrep"))
                ),
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        self._apply_advanced_to_agents()
        # 电脑/浏览器控制开关改变了工具清单：重建基础 Agent 与所有会话运行时的注册表
        # （与 MCP 热生效同一套机制；运行中的轮次下一次调用时自然使用新清单）
        self._apply_registry_to_agents()
        self.tasks.set_restrict_to_workdir(self.cfg.restrict_to_workdir)

        # 全局唤起热键改键：存 ui.json 的 hotkey 键（白名单校验在 desktop._parse_hotkey，
        # 非法值注册时静默回退默认）。生效在下次启动（热键线程随窗口创建）。空串 = 回默认。
        hotkey = params.get("hotkey")
        if hotkey is not None:
            combo = str(hotkey).strip()
            try:
                await self._write_ui_prefs({"hotkey": combo or None})
            except Exception:  # noqa: BLE001
                pass

        autostart = params.get("autostart")
        if autostart is not None:
            from ... import startup

            try:
                startup.set_enabled(bool(autostart))
            except Exception as e:  # noqa: BLE001 - 写注册表失败：回可读错误，设置项不落
                raise RuntimeError(f"开机自启设置失败：{e}") from None
        return self.advanced_settings()

    # ---- 本地目录 / 诊断（设置页的系统集成入口） ----

    def _home_subdir(self, kind: str) -> Path:
        """设置页允许打开的固定目录（kind 是枚举，不收任意路径）。"""
        base = skysheep_home()
        mapping = {
            "home": base,
            "logs": base / "logs",
            "backups": base / "backups",
            "skills": base / "skills",
            "workdir": self.working_dir,
        }
        target = mapping.get(str(kind or ""))
        if target is None:
            raise RuntimeError("未知的目录类型：" + str(kind))
        if target == self.working_dir and self.working_dir is None:
            raise RuntimeError(
                "当前没有项目，没有工作目录可打开——先在侧栏「项目」区点 ＋ 添加项目。"
            )
        return target

    def open_path(self, kind: str) -> dict:
        """用系统文件管理器打开一个固定目录（日志/数据/工作目录）。"""
        from ... import support

        target = self._home_subdir(kind)
        try:
            support.open_folder(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开目录失败：{e}") from None
        return {"path": str(target)}

    def open_external(self, target: str, local: bool = True) -> dict:
        """用系统默认程序打开一个 http(s) 链接（反馈页/下载页/注册页）。

        只接受 http(s) URL；目标由前端界面写死或用户在向导里点选，
        不作为任意跳转接口。远端调用（审查 P1-3 收口）不在服务端代开
        ——那会让持令牌设备在别人桌面上弹任意网页——而是把 URL 原样
        返回（remote=True），由远端自己的浏览器打开。
        """
        from ... import support

        target = str(target or "").strip()
        if not target.startswith(("http://", "https://")):
            raise RuntimeError("只允许打开 http(s) 链接")
        if not local:
            return {"url": target, "remote": True}
        try:
            support.open_external(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开链接失败：{e}") from None
        return {"url": target}

    # ---- 首启激活：演示模式（无 Key 用户的免注册出路） ----

    async def demo_enable(self) -> dict:
        """开启「演示模式」：写入 kind=fake 的 demo 服务并切换过去（无需任何 Key）。

        FakeProvider 脚本化回放一轮真实工具调用（list_dir），让用户在配 Key 之前
        看到完整 Agent 工作方式；正式使用时在设置里删除该服务或直接配 Key 切换。
        """
        update_provider_in_config(
            "demo",
            kind="fake",
            model="演示模式",
            api_key="demo",
            set_default=True,
            supports_vision=False,
            supports_reasoning=False,
        )
        self.cfg = load_config()
        await self.switch_model("demo")
        return {"provider": "demo", "model": "演示模式"}

    def export_diagnostics(self) -> dict:
        """打包诊断 zip（版本/环境/日志/打码后的配置）并打开所在目录。"""
        from ... import support

        try:
            target = support.build_diagnostic_zip(skysheep_home())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"生成诊断包失败：{e}") from None
        try:
            support.reveal(target)
        except Exception:  # noqa: BLE001 - 打不开文件夹不是失败
            pass
        return {"path": str(target), "size": target.stat().st_size}

    async def open_workspace_file(self, path: str) -> dict:
        """用系统默认程序打开工作目录内的文件（文件预览面板的「打开」按钮）。

        只接受工作目录内的路径；可执行/脚本类扩展名一律退化为「在资源管理器里
        定位」，避免点一下文件名就把工作目录里的脚本跑起来。
        """
        from ... import support

        if self.working_dir is None:
            raise RuntimeError(
                "当前没有项目，文件预览不可用——先在侧栏「项目」区点 ＋ 添加项目。"
            )
        raw = str(path or "").strip()
        if not raw:
            raise RuntimeError("缺少文件路径")
        target = Path(raw)
        if not target.is_absolute():
            target = self.working_dir / target
        try:
            target = target.resolve()
            target.relative_to(Path(self.working_dir).resolve())
        except (OSError, ValueError):
            raise RuntimeError("只能打开工作目录内的文件") from None
        try:
            action = support.open_with_default_app(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开失败：{e}") from None
        return {"path": str(target), "action": action}

