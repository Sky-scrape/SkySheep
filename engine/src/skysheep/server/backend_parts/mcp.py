"""MCP 接线：配置装载路径、后台连接、差量同步/重连与服务的导入/保存/删除/启停。

从 backend.py 按职责注释整段搬入。方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ...bgtasks import spawn_bg
from ...config import skysheep_home
from ...mcp import (
    MCPInstallError,
    MCPManager,
    import_servers,
    load_mcp_configs,
    load_servers,
    mcp_config_path,
    normalize_server,
    parse_file,
    parse_snippet,
    pending_stdio_commands,
    preset_by_name,
    remove_server,
    resolve_runtime_command,
    save_servers,
)

logger = logging.getLogger("skysheep.security")


class McpMixin:
    """MCP 配置与连接管理。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- MCP：程序内导入 / 删除 / 重连 ----

    def _reload_mcp_configs(self) -> None:
        configs, warnings = load_mcp_configs(
            self._mcp_global_path(),
            self._project_mcp_path_if_trusted(),
        )
        self.mcp_configs = configs
        self.mcp_warnings = warnings

    def _mcp_global_path(self) -> Path:
        return mcp_config_path(skysheep_home())

    def _mcp_project_path(self) -> Path | None:
        """项目级 mcp.json 路径；无项目态返回 None（没有可指向的项目目录）。"""
        if self.working_dir is None:
            return None
        return self.working_dir / ".skysheep" / "mcp.json"

    async def _connect_mcp_after_boot(self, cfg_warnings: list[str]) -> None:
        """启动后的后台 MCP 连接：不阻塞「服务就绪」，连完注入注册表并广播状态。

        与 setup 共用同一台 manager；配置差量同步/全部重连/关机会先取消本任务
        （_cancel_mcp_boot_connect），避免半路的 connect_all 与它们互踩。
        """
        manager = self.mcp
        if manager is None:
            return
        try:
            tools = await manager.connect_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 单台错误已记进 statuses，这里是兜底
            logger.warning("MCP 后台连接出错：%s", e)
            tools = []
        self.mcp_tools = tools
        self.mcp_warnings = [
            *cfg_warnings,
            *(f"{name}: {st.error}" for name, st in manager.statuses.items() if st.error),
        ]
        self._apply_registry_to_agents()
        self.notify_mcp_updated()

    def notify_mcp_updated(self) -> None:
        """MCP 状态变化后广播事件（无在线连接时静默，如 CLI/测试）。"""
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(ws_emit({"kind": "mcp_updated"}))
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    async def _cancel_mcp_boot_connect(self) -> None:
        """取消启动期的后台连接并等它收尾（配置同步/全部重连/关机前调用）。

        connect_all 取消时 _connect_one 会走 _close_connection 收割半开的
        keeper 与子进程（见其 except BaseException 路径），不会留孤儿。
        """
        task = self._mcp_connect_task
        self._mcp_connect_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 收尾不挡配置同步/关机
            pass

    async def _sync_mcp_changes(self) -> list[str]:
        """按配置差量同步 MCP 连接（改完配置后调用，无需重启）。

        对比新旧配置，只连接新增/变更的服务器、断开被移除的：改一个预设不再
        把其它已连接的服务器（尤其是带鉴权、重连要重新握手的远程 HTTP）全部
        拽下来重连一遍。工具注册表每次按 manager 的最新状态整体重建——注册表
        本身只是对象图，重建不费时，费时的网络连接已经被差量化了。
        """
        await self._cancel_mcp_boot_connect()
        old = self.mcp_configs or {}
        configs, cfg_warnings = load_mcp_configs(
            self._mcp_global_path(), self._project_mcp_path_if_trusted()
        )
        if self.mcp is None:
            self.mcp = MCPManager(
                configs,
                on_tools_changed=self._on_mcp_tools_changed,
                reconnect_gate=self._mcp_reconnect_gate,
            )
        manager = self.mcp
        for name in [n for n in old if n not in configs]:
            await manager.disconnect_server(name)
            manager.forget_server(name)
        for name in [n for n in configs if n not in old or old[n] != configs[n]]:
            await manager.connect_server(name, configs[name])
        self.mcp_configs = configs
        self.mcp_tools = [t for n in configs for t in manager.tools_for(n)]
        self.mcp_warnings = cfg_warnings + [
            f"{name}: {st.error}" for name, st in manager.statuses.items() if st.error
        ]
        self._apply_registry_to_agents()
        return self.mcp_warnings

    async def _reconnect_mcp(self) -> list[str]:
        """全部重连：整台 manager 重建，顺带清零各服务器的重连计数。

        只给设置页「重连」按钮和信任状态翻转这类「用户明确要重来」的入口用；
        配置增删改走 _sync_mcp_changes 的差量路径，不惊动无关服务器。
        """
        await self._cancel_mcp_boot_connect()
        if self.mcp is not None:
            await self.mcp.shutdown()
        configs, cfg_warnings = load_mcp_configs(
            self._mcp_global_path(), self._project_mcp_path_if_trusted()
        )
        self.mcp_configs = configs
        self.mcp = MCPManager(
            configs,
            on_tools_changed=self._on_mcp_tools_changed,
            reconnect_gate=self._mcp_reconnect_gate,
        )
        self.mcp_tools = await self.mcp.connect_all()
        self.mcp_warnings = cfg_warnings + [
            f"{name}: {st.error}" for name, st in self.mcp.statuses.items() if st.error
        ]
        self._apply_registry_to_agents()
        return self.mcp_warnings

    async def _on_mcp_tools_changed(self, server_name: str) -> None:
        """MCP 服务器推送 tools/list_changed：换上最新工具清单并刷新注册表。"""
        if self.mcp is None:
            return
        self.mcp_tools = [t for n in self.mcp_configs for t in self.mcp.tools_for(n)]
        self._apply_registry_to_agents()

    @staticmethod
    def _mcp_status_list(manager: MCPManager | None) -> list[dict]:
        if manager is None:
            return []
        configs = getattr(manager, "_configs", {})
        out = []
        for n, st in manager.statuses.items():
            cfg = configs.get(n)
            # 审查 S-09：http 明文 + 鉴权头 = Bearer 凭据可被中间人截获。
            # 只给前端一个布尔，headers 内容（含密钥）不下发。
            insecure = bool(
                cfg is not None and cfg.headers
                and str(cfg.url or "").lower().startswith("http://")
            )
            out.append({
                "name": n, "connected": st.connected, "enabled": st.enabled,
                "error": st.error, "tools": st.tool_names,
                "connecting": getattr(st, "connecting", False),
                "reconnecting": getattr(st, "reconnecting", False),
                "restarts": getattr(st, "restarts", 0),
                "insecure_http": insecure,
            })
        return out

    async def import_mcp_servers(
        self,
        *,
        snippet: str = "",
        path: str = "",
        scope: str = "global",
        overwrite: bool = False,
        reconnect: bool = True,
        confirmed: bool = False,
    ) -> dict:
        """导入 MCP 服务：从粘贴的 JSON（snippet）或一个本机 .json 文件（path）。

        安全审查 M10：含 stdio（command）定义时先返回 needs_confirm + 命令清单
        让前端弹一次显式确认，confirmed=true 才真正写盘——连接即执行本机命令，
        不能粘贴一段 JSON 就等于声明允许在本机运行任意程序。
        """
        if snippet.strip():
            servers = parse_snippet(snippet)
        elif path.strip():
            servers = parse_file(path)
        else:
            raise RuntimeError("请粘贴 MCP 配置，或指定一个 .json 文件路径")
        target = self._mcp_global_path() if scope == "global" else self._mcp_project_path()
        if target is None:
            raise RuntimeError("项目级 MCP 配置需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。")
        if not confirmed:
            pending = pending_stdio_commands(servers, target, overwrite=overwrite)
            if pending:
                return {"needs_confirm": True, "pending": pending}
        try:
            result = import_servers(servers, target, overwrite=overwrite, confirmed=True)
        except MCPInstallError as e:
            raise RuntimeError(str(e)) from e

        if reconnect and result["added"]:
            await self._sync_mcp_changes()
        result["needs_confirm"] = False
        result["scope"] = scope
        result["mcp"] = self._mcp_status_list(self.mcp)
        result["mcp_warnings"] = self.mcp_warnings
        if scope == "project":
            # 用户自己在界面上写的项目级配置；来源限定项目级 mcp.json
            self.trust.refresh(touched=self._mcp_project_path())
        self._annotate_untrusted_project_scope(result, scope)
        if result["added"]:
            result["hint"] = "已接入，可直接对话使用" + (
                "（有服务没连上时看下面的错误信息）" if self.mcp_warnings else ""
            )
        elif result["skipped"]:
            result["hint"] = "同名服务已存在，勾选「覆盖同名服务」后重试即可替换"
        return result

    async def save_mcp_server(
        self,
        name: str,
        *,
        command: str = "",
        args: list[str] | None = None,
        url: str = "",
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        readonly: bool = False,
        timeout: float = 0.0,
        scope: str = "global",
        overwrite: bool = True,
    ) -> dict:
        """手工填写一个 MCP 服务（分字段表单），存进 mcp.json。"""
        raw: dict = {}
        if command.strip():
            raw["command"] = command.strip()
            if args:
                raw["args"] = [str(a) for a in args if str(a).strip()]
        if url.strip():
            raw["url"] = url.strip()
        if env:
            raw["env"] = {str(k): str(v) for k, v in env.items()}
        # 请求头仅对 HTTP 传输有意义：stdio 服务带上它只会让人困惑
        if headers and url.strip():
            raw["headers"] = {
                str(k): str(v) for k, v in headers.items() if str(k).strip()
            }
        if readonly:
            raw["readonly"] = True
        if timeout and timeout > 0:
            raw["timeout"] = timeout
        try:
            cfg = normalize_server(raw)
            target = self._mcp_global_path() if scope == "global" else self._mcp_project_path()
            if target is None:
                raise RuntimeError(
                    "项目级 MCP 配置需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。"
                )
            # 表单没有「停用」字段：覆盖一个已停用的同名服务时保留停用状态，
            # 否则只是改个参数保存，服务就被顺手重新启用并拉起（stdio 等于
            # 立即执行本机命令），超出用户这次操作表达的意思
            prior = load_servers(target).get(name.strip())
            if isinstance(prior, dict) and prior.get("enabled") is False:
                raw["enabled"] = False
                cfg = normalize_server(raw)
            # 分字段表单：用户看着 command/args 输入框亲手填的，保存动作本身
            # 就是明确意图（M10 的确认只针对粘贴/导入这种「命令不可见」的路径）
            result = import_servers(
                {name.strip(): cfg}, target, overwrite=overwrite, confirmed=True
            )
        except MCPInstallError as e:
            raise RuntimeError(str(e)) from e
        if result["added"]:
            await self._sync_mcp_changes()
        result["scope"] = scope
        result["mcp"] = self._mcp_status_list(self.mcp)
        result["mcp_warnings"] = self.mcp_warnings
        if scope == "project":
            # 同上：用户自己的改动延续既有信任，来源限定项目级 mcp.json
            self.trust.refresh(touched=self._mcp_project_path())
        self._annotate_untrusted_project_scope(result, scope)
        return result

    def _annotate_untrusted_project_scope(self, result: dict, scope: str) -> None:
        """项目级配置写完了但还没信任本项目时，把「为什么没连上」说清楚。

        否则用户会在设置页加了服务却看不到连接，以为是坏了。这里只补一句提示，
        不自动授信任——项目里可能同时还躺着别的（仓库带来的）服务，一并放行
        不是用户在这一次操作里表达的意思。
        """
        if scope != "project" or self.trust.is_trusted():
            return
        result["needs_trust"] = True
        result["hint"] = (
            "已写入项目配置，但本项目尚未信任：项目自带的配置在确认前不会自动执行。"
            "在顶部横幅点「信任本项目」后即会连接。"
        )

    async def add_mcp_preset(self, name: str, scope: str = "global") -> dict:
        """一键添加内置预设 MCP 服务：按预设原文写入 mcp.json 并立即连接。

        args 里的 {dir} 占位符替换为当前工作目录；command 先经
        resolve_runtime_command 解析（uv 装在官方默认落点但不在 PATH 时写绝对
        路径）。同名已存在时不覆盖（save_mcp_server 用 overwrite=False，
        走 skipped 通道）。
        """
        preset = preset_by_name(name)
        if preset is None:
            raise RuntimeError(f"没有这个内置预设：{name}")
        if self.working_dir is None and any("{dir}" in str(a) for a in preset["args"]):
            raise RuntimeError(
                "该预设要用当前项目的工作目录——先在侧栏「项目」区点 ＋ 添加项目。"
            )
        args = [str(a).replace("{dir}", str(self.working_dir or "")) for a in preset["args"]]
        result = await self.save_mcp_server(
            preset["name"],
            # uv 装在官方默认落点但不在 PATH 时解析成绝对路径，启动不依赖 PATH
            command=resolve_runtime_command(preset["command"]),
            args=args,
            readonly=preset["readonly"],
            scope=scope,
            overwrite=False,
        )
        result["preset"] = preset["name"]
        if result.get("skipped"):
            result["hint"] = f"「{preset['label']}」已经添加过了，直接用即可（或删除后重新添加）"
        elif result.get("added"):
            st = next((m for m in result["mcp"] if m["name"] == preset["name"]), None)
            if st and st["connected"]:
                result["hint"] = (
                    f"✓ 已添加「{preset['label']}」，连上 {len(st['tools'])} 个工具，可直接对话使用"
                )
            elif st:
                result["hint"] = f"已添加「{preset['label']}」，但没连上：{st['error'] or '未知错误'}"
        return result

    def mcp_installed_names(self) -> list[str]:
        """当前 mcp.json（全局+项目）里已有的服务名，前端据此标「已添加」。"""
        return sorted(self.mcp_configs.keys())

    async def delete_mcp_server(self, name: str, scope: str = "global") -> dict:
        """删除一个 MCP 服务并断开它的连接。"""
        project_path = self._mcp_project_path()
        path = self._mcp_global_path() if scope == "global" else project_path
        if path is None:
            raise RuntimeError("项目级 MCP 配置需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。")
        try:
            result = remove_server(name, path)
        except MCPInstallError as e:
            # 全局/项目两边都试试，用户不必知道它当初存在哪
            other = project_path if scope == "global" else self._mcp_global_path()
            if other is None:
                raise RuntimeError(str(e)) from e
            try:
                result = remove_server(name, other)
                path = other
            except MCPInstallError:
                raise RuntimeError(str(e)) from e
        await self._sync_mcp_changes()
        result["scope"] = "project" if path == project_path else "global"
        result["mcp"] = self._mcp_status_list(self.mcp)
        return result

    async def set_mcp_enabled(self, name: str, enabled: bool, scope: str = "global") -> dict:
        """停用/启用一个 MCP 服务：配置原样保留（env/headers 不用重填），停用只是不连接。

        找配置文件时按请求的 scope 优先、另一边兜底（与 delete_mcp_server 一致，
        用户不必知道服务当初存在哪边）。
        """
        project_path = self._mcp_project_path()
        first = self._mcp_global_path() if scope == "global" else project_path
        second = project_path if scope == "global" else self._mcp_global_path()
        candidates = [p for p in (first, second) if p is not None]
        hit: Path | None = None
        for p in candidates:
            servers = load_servers(p)
            if name not in servers:
                continue
            section = dict(servers[name])
            if enabled:
                section.pop("enabled", None)  # 默认即启用，不写冗余字段
            else:
                section["enabled"] = False
            servers[name] = section
            save_servers(p, servers)
            hit = p
            break
        if hit is None:
            raise RuntimeError(f"找不到 MCP 服务：{name}")
        await self._sync_mcp_changes()
        result: dict = {
            "name": name,
            "enabled": enabled,
            "scope": "project" if hit == project_path else "global",
            "mcp": self._mcp_status_list(self.mcp),
            "mcp_warnings": self.mcp_warnings,
        }
        if not enabled:
            result["hint"] = f"已停用「{name}」：它的工具已从 Agent 移除，配置保留，随时可重新启用"
        else:
            st = next((m for m in result["mcp"] if m["name"] == name), None)
            if st and st["connected"]:
                result["hint"] = f"✓ 已启用「{name}」，连上 {len(st['tools'])} 个工具"
            else:
                result["hint"] = f"已启用「{name}」，但没连上：{(st or {}).get('error') or '未知错误'}"
        return result
