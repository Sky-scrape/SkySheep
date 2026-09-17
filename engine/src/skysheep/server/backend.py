"""ServerBackend：把引擎接线（会话/模型/技能/MCP/子代理/权限）暴露给服务层。

与 CLI 的 ChatApp 共享同一套引擎 API，但不含任何 UI 逻辑——
FastAPI WebSocket 端点消费它并转发事件流。
"""

from __future__ import annotations

import asyncio
import codecs
import difflib
import hashlib
import ipaddress
import json
import locale
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import __version__
from ..config import (
    PRESET_SIGNUP_URLS,
    PRESETS,
    REASONING_EFFORT_LABELS,
    REASONING_EFFORTS,
    ConfigError,
    ProviderConfig,
    SkySheepConfig,
    add_provider_to_config,
    config_path,
    db_path,
    disable_provider_in_config,
    disabled_providers_in_config,
    load_config,
    remove_provider_from_config,
    resolve_api_key,
    resolve_imagegen,
    resolve_websearch,
    restore_provider_in_config,
    set_advanced_settings_in_config,
    set_provider_models_in_config,
    set_subagent_settings_in_config,
    skysheep_home,
    update_config_section,
    update_provider_in_config,
)
from ..core import Agent, build_system_prompt
from ..core.checkpoints import CheckpointStore
from ..core.context import compact_history
from ..core.hooks import HookRunner, hooks_from_config, load_raw_config
from ..core.prompt import (
    MAX_INSTRUCTIONS_CHARS,
    PLAN_MODE_PREFIX,
    load_project_instructions,
    render_instructions_section,
)
from ..core.roundtable import MemberSpec, RoundtableOutcome, run_roundtable
from ..core.subagent import CheckTaskTool, SpawnAgentTool, TaskManager
from ..core.subagent_store import (
    BUILTIN_AGENT_TYPES,
    BUILTIN_DISPLAY,
    SubagentDef,
    SubagentDefError,
    SubagentStore,
    validate_subagent_name,
)
from ..core.uptodate import fetch_latest_release as check_latest_release
from ..core.uptodate import is_newer_version
from ..events import (
    AssistantMessage,
    ErrorEvent,
    QueueUpdated,
    TurnFinished,
    TurnStarted,
)
from ..mcp import (
    MCPInstallError,
    MCPManager,
    import_servers,
    load_mcp_configs,
    mcp_config_path,
    normalize_server,
    parse_file,
    parse_snippet,
    preset_by_name,
    presets_public,
    remove_server,
)
from ..messages import ImageBlock, Message, TextBlock
from ..messages import system_text as history_system_text
from ..models import Provider
from ..models.base import ProviderDone, ProviderReasoning, ProviderTextDelta
from ..models.factory import build_provider
from ..models.probe import probe_provider_models
from ..security.gate import HeadlessGate, PermissionGate
from ..session import SessionStore
from ..skills import SkillLoader
from ..skills.installer import SkillInstallError, install_from_url, remove_skill
from ..skills.installer import install as install_skill
from ..skills.market import fetch_market_index
from ..tools import ChangeRecorder, Safety, ToolRegistry, default_tools
from ..tools.memory import render_memory_section
from ..tools.skill import LoadSkillTool

EmitFn = Callable[[dict], Awaitable[None]]

MAX_DIFF_CHARS = 8000


def _decode_bytes(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _unified_diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="改前", tofile="改后", lineterm=""
        )
    )


@dataclass
class QueuedTurn:
    """Agent 工作期间用户继续发来的消息：本轮结束后自动依次执行。"""

    text: str
    emit: EmitFn
    plan_mode: bool
    fut: asyncio.Future = field(repr=False)
    roundtable: bool = False
    members: list | None = None  # 圆桌成员 [{provider, model}]，None=默认策略
    images: list[ImageBlock] = field(default_factory=list)  # 本轮图片附件（圆桌轮忽略）

    def resolve(self, result: dict) -> None:
        if not self.fut.done():
            self.fut.set_result(result)

    def fail(self, e: BaseException) -> None:
        if not self.fut.done():
            self.fut.set_exception(e)


def _msg_brief(m: Message) -> dict:
    """历史消息的轻量 JSON（前端渲染历史用）：role + 文本 + 图片；工具轮略。"""
    return {
        "role": m.role,
        "text": m.text,
        "seq": m.seq,
        "images": [
            {"media_type": b.media_type, "data": b.data}
            for b in m.content if getattr(b, "type", "") == "image"
        ],
        "roundtable": m.roundtable,
        # 思考型模型的推理内容（前端渲染为可折叠块）；其他角色为空串
        "thinking": "".join(b.text for b in m.content if getattr(b, "type", "") == "thinking"),
    }


# 无人值守门控：定时任务与 headless run 共用 security.gate.HeadlessGate
CronGate = HeadlessGate


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 会话导出 HTML 模板：自包含单文件（无外部资源），纸墨配色与桌面端一致
EXPORT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · SkySheep 导出</title>
<style>
  body {{ background: #F5EFDE; color: #1D1A16;
          font: 15px/1.75 "Microsoft YaHei", system-ui, sans-serif; margin: 0; }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 40px 20px 80px; }}
  header {{ border-bottom: 2px solid #1D1A16; padding-bottom: 14px; margin-bottom: 26px; }}
  header h1 {{ font-size: 22px; margin: 0 0 6px; }}
  header .meta {{ color: #6b6459; font-size: 13px; }}
  .msg {{ border: 1.5px solid #c9bfa5; border-radius: 10px; padding: 12px 16px; margin: 12px 0;
          background: #fffdf6; white-space: pre-wrap; overflow-wrap: break-word;
          box-shadow: 0 1px 3px rgba(29,26,22,.08); }}
  .msg.user {{ background: #E8DFC7; border-color: #1D1A16; margin-left: 48px; }}
  .msg.ai {{ margin-right: 48px; }}
  .msg.tool {{ background: #faf7ee; color: #57503f; font-size: 13px; }}
  .msg.tool.err {{ border-color: #b03a2e; }}
  .msg pre {{ margin: 6px 0 0; white-space: pre-wrap; font: 12px/1.6 Consolas, monospace; }}
</style>
</head>
<body>
<div class="wrap">
<header><h1>{title}</h1><div class="meta">由 SkySheep v{version} 导出于 {date} · 纯本地会话记录</div></header>
{body}
</div>
</body>
</html>
"""


@dataclass
class SessionRuntime:
    """一个会话的运行时状态：独立的 Agent、文件改动记录器、消息队列与运行任务。

    多会话并行的基础：每个会话的 turn 在自己的 runtime 里跑，互不占用；
    事件发出时带 session_id，前端按标签路由。
    """

    sid: str
    agent: Agent
    recorder: ChangeRecorder
    queue: list = field(default_factory=list)
    run_task: asyncio.Task | None = None


class TerminalManager:
    """右侧「终端」标签页的后端：在工作目录里执行用户手敲的命令，流式回传输出。

    与 run_command 工具的两点不同：命令由用户亲自输入，不经过权限门；
    输出走事件流实时回显（同一条命令 stdout/stderr 交错出现），并随时可停。
    同一时间只运行一条命令（界面有运行态与「停止」按钮）。
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._stop_requested = False

    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def run(self, command: str, cwd: Path, emit: EmitFn) -> dict:
        if self.busy():
            raise RuntimeError("已有命令在运行，请先等它结束或点「停止」")
        if sys.platform == "win32":
            argv = ["cmd.exe", "/d", "/s", "/c", command]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            argv = ["/bin/bash", "-c", command]
            flags = 0
        # 中文 Windows 的 cmd 工具链默认输出 ANSI 代码页（cp936），按本机编码解码
        enc = locale.getpreferredencoding(False) or "utf-8"
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - exe 固定为 cmd/bash，命令经权限外的人工输入
                argv,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as e:
            self.proc = None
            raise RuntimeError(f"无法启动命令：{e}") from None

        proc = self.proc
        decoders = {name: codecs.getincrementaldecoder(enc)(errors="replace") for name in ("out", "err")}

        async def pump(stream, name: str) -> None:
            # read1：管道里有多少就回多少，不凑满缓冲区，保证输出实时到达
            try:
                while True:
                    chunk = await asyncio.to_thread(stream.read1, 4096)
                    if not chunk:
                        break
                    text = decoders[name].decode(chunk)
                    if text:
                        await emit({"kind": "terminal_chunk", "stream": name, "text": text})
            except Exception:  # noqa: BLE001 - 客户端断开时丢弃输出，进程照常跑完
                return

        t_out = asyncio.create_task(pump(proc.stdout, "out"))
        t_err = asyncio.create_task(pump(proc.stderr, "err"))
        await asyncio.to_thread(proc.wait)
        # 先短暂等读取任务把尾部输出排干；有残留子进程攥着管道时最多再等 5 秒
        _, pending = await asyncio.wait({t_out, t_err}, timeout=5)
        for t in pending:
            t.cancel()
        code = proc.returncode
        stopped = self._stop_requested
        self.proc = None
        self._stop_requested = False
        try:
            await emit({"kind": "terminal_done", "code": code, "stopped": stopped})
        except Exception:  # noqa: BLE001
            pass
        return {"code": code, "stopped": stopped}

    def stop(self) -> bool:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return False
        self._stop_requested = True
        try:
            if sys.platform == "win32":
                # 树杀：cmd /c 起的子进程要一并结束
                subprocess.run(  # noqa: S603 - exe 固定为 taskkill
                    ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=5,
                )
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return True


class ServerBackend:
    def __init__(
        self,
        working_dir: str | Path = ".",
        provider_name: str | None = None,
        provider_factory: Callable[[], Provider] | None = None,
        store: SessionStore | None = None,
    ) -> None:
        self.working_dir = Path(working_dir).resolve()
        self.provider_name_arg = provider_name
        self._provider_factory_override = provider_factory
        self._store_override = store
        self.cfg: SkySheepConfig | None = None
        self.store: SessionStore | None = None
        self.project = None
        self.session = None
        self.provider: Provider | None = None
        # agent/queue/_run_task/_recorder 是 property（指向活动会话 runtime）；
        # 基底实例供无会话时兑底与全局刷新，runtimes 在 __init__ 尾部创建
        self._base_agent: Agent | None = None
        self._base_queue: list = []
        self._base_run_task: asyncio.Task | None = None
        self._base_recorder: ChangeRecorder | None = None
        self.runtimes: dict[str, SessionRuntime] = {}
        self.gate: PermissionGate | None = None
        self.mcp: MCPManager | None = None
        self.mcp_configs: dict = {}
        self.mcp_tools: list = []
        self.skills: SkillLoader | None = None
        self.tasks: TaskManager | None = None
        self.subagent_store = SubagentStore(skysheep_home() / "subagents.json")
        self.instructions_file: str | None = None
        self.instructions_text: str = ""
        self.mcp_warnings: list[str] = []
        self.checkpoints = CheckpointStore()
        self.hooks: HookRunner | None = None
        self.term = TerminalManager()
        self.aux_history: list[Message] = []
        self.ws_emitters: list = []  # 在线 WS 连接的 emit 函数（提醒/日程广播用）
        self._reminder_task: asyncio.Task | None = None
        self._cron_task: asyncio.Task | None = None
        self._cron_running: set[int] = set()  # 正在跑的定时任务 id（防重复触发）
        self._titling: set[str] = set()  # 正在自动生成标题的会话
        self.update_info: dict | None = None  # {"version","url","notes"}：发现的新版本
        self.update_error: str | None = None  # 手动检查时的失败原因（进设置 · 关于）
        self._market_cache: tuple[float, dict] | None = None  # 技能广场索引缓存

    # ---- 多会话运行时：agent/queue/_run_task/_recorder 指向活动会话的 runtime，
    # ---- 后台会话通过 runtimes[sid] 直接访问（并行 turn 不经 property）。

    def _get_runtime(self, session_id: str) -> SessionRuntime:
        """取（或懒建）一个会话的运行时；新 runtime 自带系统提示词与完整工具集。"""
        rt = self.runtimes.get(session_id)
        if rt is None:
            recorder = ChangeRecorder()
            rt = SessionRuntime(
                sid=session_id,
                agent=Agent(
                    provider=self.provider,
                    registry=self._build_full_registry(recorder),
                    gate=self.gate,
                    working_dir=self.working_dir,
                    max_iterations=self.cfg.max_iterations,
                    context_limit_tokens=self._context_limit(),
                    compaction_keep_recent=self.cfg.compaction_keep_recent,
                    hooks=self.hooks,
                    restrict_to_workdir=self.cfg.restrict_to_workdir,
                ),
                recorder=recorder,
            )
            rt.agent.set_system(self.compose_system())
            self.runtimes[session_id] = rt
        return rt

    # ---- 当前服务的模型能力（上下文窗口 / 图片输入） ----

    def _current_provider_cfg(self):
        """当前服务的 ProviderConfig（没有就返回 None）。"""
        if not self.provider_name:
            return None
        return self.cfg.providers.get(self.provider_name)

    def _context_limit(self) -> int:
        """上下文上限：服务级设置优先，否则用全局默认。"""
        pc = self._current_provider_cfg()
        if pc is None:
            return self.cfg.context_limit_tokens
        return pc.effective_context_limit(self.cfg.context_limit_tokens)

    def _supports_vision(self) -> bool:
        """当前服务是否支持图片输入（未配置的服务按支持处理，不拦人）。"""
        pc = self._current_provider_cfg()
        return True if pc is None else bool(pc.supports_vision)

    @property
    def agent(self) -> Agent:
        """活动会话的 Agent（无会话时用基底实例克底，仅供只读探针）。"""
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.agent if rt else self._base_agent

    @property
    def queue(self) -> list:
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.queue if rt else self._base_queue

    @queue.setter
    def queue(self, v: list) -> None:
        self._base_queue = v

    @property
    def _run_task(self):
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.run_task if rt else self._base_run_task

    @_run_task.setter
    def _run_task(self, v):
        if self.session and self.session.id in self.runtimes:
            self.runtimes[self.session.id].run_task = v
        else:
            self._base_run_task = v

    @property
    def _recorder(self) -> ChangeRecorder:
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.recorder if rt else self._base_recorder

    @_recorder.setter
    def _recorder(self, v: ChangeRecorder) -> None:
        self._base_recorder = v

    @staticmethod
    def _forget_runtime(rt: SessionRuntime) -> None:
        """runtime 已从字典摘除后调用：释放它引用的全局资源（防泄漏）。"""
        if getattr(rt.agent, "registry", None) is not None:
            rt.agent.registry = None

    def _for_each_agent(self):
        """所有 runtime 的 agent（含基底），用于全局状态刷新（模型/注册表/系统词）。"""
        yield self._base_agent
        for rt in self.runtimes.values():
            yield rt.agent

    # ---- 生命周期 ----

    async def setup(self) -> None:
        if not self.working_dir.exists():
            raise RuntimeError("directory not found: " + str(self.working_dir))
        self.cfg = load_config()
        # hooks（工具调用前后的用户钩子，config.toml [hooks]）
        raw_cfg = load_raw_config()
        pre_rules, post_rules = hooks_from_config(raw_cfg)
        self.hooks = HookRunner(pre_rules, post_rules, working_dir=self.working_dir) \
            if (pre_rules or post_rules) else None
        # 检查点落盘：按项目隔离目录（切项目互不可见，重启后仍可回滚）
        self.checkpoints = CheckpointStore(root=self._checkpoint_root())
        self.store = self._store_override or await SessionStore(db_path()).connect()
        self.project = await self.store.get_or_create_project(str(self.working_dir))
        self.gate = PermissionGate(store=self.store, project_id=self.project.id,
                                   working_dir=self.working_dir)
        await self.gate.load_project_rules()
        # 分级权限模式：上次会话选的「自动允许写入」在重启后保持
        self.gate.auto_accept_write = self._read_ui_prefs().get("accept_edits", 0) == 1
        # 缺 API Key 不阻塞启动：记录状态，界面里可见/可切换后再用
        self.provider_error: str | None = None
        try:
            self.provider = self._build_provider(self.provider_name_arg or self.cfg.default)
        except RuntimeError as e:
            self.provider_error = str(e)
            self.provider = None
            self.provider_name = ""
            self.provider_model = ""

        mcp_configs = load_mcp_configs(self._mcp_global_path(), self._mcp_project_path())
        self.mcp_configs = mcp_configs
        self.mcp = MCPManager(mcp_configs)
        self.mcp_tools = await self.mcp.connect_all()
        self.mcp_warnings = [
            f"{name}: {st.error}"
            for name, st in self.mcp.statuses.items()
            if st.error
        ]

        self.skills = SkillLoader(
            global_dir=skysheep_home() / "skills",
            project_dir=self.working_dir / ".skysheep" / "skills",
            state_path=self.working_dir / ".skysheep" / "skills.json",
        )
        self.skills.discover()

        # 项目说明（对标 Codex AGENTS.md / Claude Code CLAUDE.md）
        self.instructions_file, self.instructions_text = load_project_instructions(self.working_dir)

        self.subagent_store.load()
        self.tasks = TaskManager(
            provider_factory=lambda: self.provider,
            working_dir=self.working_dir,
            max_iterations=self.cfg.subagent_max_iterations,
            store=self.subagent_store,
            provider_resolver=self._subagent_provider,
            registry_resolver=self._subagent_registry,
        )

        self._base_agent = Agent(
            provider=self.provider,
            registry=self._build_full_registry(),
            gate=self.gate,
            working_dir=self.working_dir,
            max_iterations=self.cfg.max_iterations,
            context_limit_tokens=self._context_limit(),
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            hooks=self.hooks,
            restrict_to_workdir=self.cfg.restrict_to_workdir,
        )
        # 后台查一次新版本：几秒超时、失败完全静默，结果随 boot 快照到前端
        self._update_task = asyncio.create_task(self._check_update_quietly())
        await self.open_initial_session()

    # ---- 新功能配置解析 / 检查点目录 ----

    def _checkpoint_root(self) -> Path:
        """当前项目的检查点目录（按工作目录指纹隔离）。"""
        tag = hashlib.sha256(str(self.working_dir).lower().encode("utf-8")).hexdigest()[:12]
        return skysheep_home() / "backups" / "checkpoints" / tag

    def _websearch_kwargs(self) -> dict | None:
        try:
            return resolve_websearch(self.cfg)
        except Exception:  # noqa: BLE001
            return None

    def _imagegen_kwargs(self) -> dict | None:
        try:
            return resolve_imagegen(self.cfg)
        except Exception:  # noqa: BLE001
            return None

    async def _check_update_quietly(self) -> None:
        from ..core.uptodate import DEFAULT_RELEASES_API

        try:
            info = await check_latest_release(DEFAULT_RELEASES_API)
            if is_newer_version(info["version"], __version__):
                self.update_info = info
        except Exception:  # noqa: BLE001 - 离线/仓库不存在：完全静默
            pass

    def _build_full_registry(self, recorder: ChangeRecorder | None = None) -> ToolRegistry:
        """完整工具集：内置（write/edit/画图挂检查点记录器）+ 日程 + 技能 + 子代理 + MCP。

        子代理可在设置页整体关闭：关掉后 spawn_agent / check_task 不注册，
        模型看不到这两个工具（配置里 subagent_enabled = false）。
        """
        registry = ToolRegistry(default_tools(
            recorder=recorder or self._recorder,
            store=self.store,
            websearch=self._websearch_kwargs(),
            imagegen=self._imagegen_kwargs(),
            computer_control=self.cfg.computer_control,
            browser_control=self.cfg.browser_control,
        ))
        registry.register(LoadSkillTool(self.skills))
        if self.cfg.subagent_enabled:
            registry.register(SpawnAgentTool(self.tasks))
            registry.register(CheckTaskTool(self.tasks))
        for t in self.mcp_tools:
            registry.register(t)
        return registry

    def _apply_registry_to_agents(self, recorder: ChangeRecorder | None = None) -> None:
        """把按当前配置重建的工具集发给基础 Agent 与所有会话运行时。"""
        self._base_agent.registry = self._build_full_registry()
        for rt in self.runtimes.values():
            rt.agent.registry = self._build_full_registry(rt.recorder)

    async def shutdown(self) -> None:
        for rt in self.runtimes.values():
            t = rt.run_task
            if t and not t.done():
                t.cancel()
        self.cancel_run()
        self.stop_cron_loop()
        self.term.stop()
        self.stop_reminder_loop()
        if self.mcp:
            await self.mcp.shutdown()
        if self.tasks:
            self.tasks.cancel_all()
        if self.store and self._store_override is None:
            await self.store.close()

    # ---- 日程：增删改查 + 到点提醒广播 ----

    async def schedule_add(self, params: dict) -> dict:
        title = str(params.get("title", "")).strip()
        if not title:
            raise RuntimeError("日程标题不能为空")
        start_at = params.get("start_at")
        if start_at is None:
            raise RuntimeError("缺少开始时间 start_at")
        row = await self.store.add_schedule(
            title,
            float(start_at),
            notes=str(params.get("notes", "")),
            remind=bool(params.get("remind", True)),
            remind_before=int(params.get("remind_before", 0) or 0),
        )
        self.notify_schedule_changed()
        return row

    async def schedule_update(self, params: dict) -> dict:
        sid = int(params.get("id", 0))
        kw: dict = {}
        if params.get("title") is not None:
            kw["title"] = str(params["title"]).strip()
        if params.get("notes") is not None:
            kw["notes"] = str(params["notes"])
        if params.get("start_at") is not None:
            kw["start_at"] = float(params["start_at"])
        if params.get("remind") is not None:
            kw["remind"] = bool(params["remind"])
        if params.get("remind_before") is not None:
            kw["remind_before"] = int(params["remind_before"])
        if params.get("done") is not None:
            kw["done"] = bool(params["done"])
        if not kw:
            raise RuntimeError("没有给出任何要修改的字段")
        row = await self.store.update_schedule(sid, **kw)
        if not row:
            raise RuntimeError("日程不存在")
        self.notify_schedule_changed()
        return row

    async def schedule_delete(self, schedule_id: int) -> dict:
        if not await self.store.delete_schedule(schedule_id):
            raise RuntimeError("日程不存在")
        self.notify_schedule_changed()
        return {"deleted": schedule_id}

    def notify_schedule_changed(self) -> None:
        """日程变化后广播事件（无在线连接时静默，如 CLI/测试）。"""
        for ws_emit in list(self.ws_emitters):
            try:
                asyncio.create_task(
                    ws_emit({"kind": "schedule_updated"})
                )
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    # ---- 到点提醒循环 ----

    REMINDER_INTERVAL = 20.0  # 秒

    def start_reminder_loop(self) -> None:
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    def stop_reminder_loop(self) -> None:
        if getattr(self, "_reminder_task", None):
            self._reminder_task.cancel()
            self._reminder_task = None

    async def _reminder_loop(self) -> None:
        """周期扫描到点未提醒的日程，向所有在线前端广播提醒事件。
        应用关闭期间的到期日程会在下次启动扫描时补提醒。"""
        while True:
            try:
                await self._reminder_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.REMINDER_INTERVAL)

    async def _reminder_pass(self) -> None:
        """单轮扫描：把到期未提醒的日程推给所有在线连接并标记已提醒。"""
        due = await self.store.due_schedules()
        for row in due:
            for ws_emit in list(self.ws_emitters):
                try:
                    await ws_emit({"kind": "schedule_reminder", **row})
                except Exception:
                    pass
            await self.store.mark_schedule_reminded(row["id"])

    # ---- 定时任务：无人值守的周期 Agent 运行 ----

    CRON_INTERVAL = 20  # 扫描周期（秒）

    def start_cron_loop(self) -> None:
        self._cron_task = asyncio.create_task(self._cron_loop())

    def stop_cron_loop(self) -> None:
        if getattr(self, "_cron_task", None):
            self._cron_task.cancel()
            self._cron_task = None

    async def _cron_loop(self) -> None:
        """周期扫描到点的定时任务，逐个后台运行。"""
        while True:
            try:
                await self._cron_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.CRON_INTERVAL)

    async def _cron_pass(self) -> None:
        for row in await self.store.due_cron_tasks():
            if row["id"] in self._cron_running:
                continue
            # 后台触发：任务跑多久都不阻塞扫描循环（下一轮扫描跳过 in-flight 任务）
            asyncio.get_running_loop().create_task(self._run_cron_task(row["id"]))

    def _broadcast_cron(self, task: dict) -> None:
        for ws_emit in list(self.ws_emitters):
            try:
                asyncio.get_running_loop().create_task(
                    ws_emit({"kind": "cron_updated", "task": task})
                )
            except Exception:
                pass

    async def cron_list(self) -> dict:
        tasks = await self.store.list_cron_tasks(self.project.id)
        return {"tasks": tasks}

    async def cron_add(self, params: dict) -> dict:
        name = str(params.get("name", "")).strip()[:40] or "未命名任务"
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise RuntimeError("任务指令不能为空")
        stype = str(params.get("schedule_type", "interval"))
        if stype not in ("interval", "daily", "weekly"):
            raise RuntimeError("schedule_type 只支持 interval / daily / weekly")
        interval = max(1, int(params.get("interval_minutes") or 1))
        tod = str(params.get("time_of_day") or "")
        wd = int(params.get("weekday") or -1)
        tools = [str(t).strip() for t in (params.get("allowed_tools") or []) if str(t).strip()]
        task = await self.store.add_cron_task(
            self.project.id, name, prompt, stype, interval, tod, wd, tools
        )
        task = await self.store.update_cron_task(
            task["id"], next_run_at=self.store.compute_next_run(task)
        )
        self._broadcast_cron(task)
        return task

    async def cron_update(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        kw = {}
        if params.get("name") is not None:
            kw["name"] = str(params["name"]).strip()[:40] or task["name"]
        if params.get("prompt") is not None:
            kw["prompt"] = str(params["prompt"]).strip()
        if params.get("schedule_type") is not None:
            stype = str(params["schedule_type"])
            if stype not in ("interval", "daily", "weekly"):
                raise RuntimeError("schedule_type 只支持 interval / daily / weekly")
            kw["schedule_type"] = stype
        if params.get("interval_minutes") is not None:
            kw["interval_minutes"] = max(1, int(params["interval_minutes"]))
        if params.get("time_of_day") is not None:
            kw["time_of_day"] = str(params["time_of_day"])
        if params.get("weekday") is not None:
            kw["weekday"] = int(params["weekday"])
        if params.get("allowed_tools") is not None:
            kw["allowed_tools"] = [str(t).strip() for t in params["allowed_tools"] if str(t).strip()]
        if params.get("enabled") is not None:
            kw["enabled"] = 1 if bool(params["enabled"]) else 0
        # 调度/启用变化后重算下次运行时间
        if any(k in kw for k in ("schedule_type", "interval_minutes", "time_of_day",
                                 "weekday", "enabled")) and "next_run_at" not in kw:
            merged = {**task, **kw}
            enabled = kw.get("enabled", task["enabled"])
            kw["next_run_at"] = self.store.compute_next_run(merged) if enabled else 0
        task = await self.store.update_cron_task(tid, **kw)
        self._broadcast_cron(task)
        return task

    async def cron_delete(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        ok = await self.store.delete_cron_task(tid)
        return {"deleted": ok, "id": tid}

    async def cron_run_now(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        await self._run_cron_task(tid, force=True)
        return {"started": True, "id": tid}

    async def _run_cron_task(self, task_id: int, force: bool = False) -> None:
        """跑一个定时任务：独立会话 + headless 门控；结果写回任务行并广播。"""
        if task_id in self._cron_running:
            return
        task = await self.store.get_cron_task(task_id)
        if task is None:
            return
        if not task["enabled"] and not force:
            return
        if not force:
            self._cron_running.add(task_id)
        try:
            if self.provider is None:
                raise RuntimeError("尚未配置可用的模型 API Key")

            # 每个任务一个独立会话（同名），历史随运行累积
            sess = await self.store.create_session(self.project.id, title=f"⏰ {task['name']}")
            sid = sess.id
            gate = CronGate(allowed=task["allowed_tools"], store=self.store,
                            project_id=self.project.id, working_dir=self.working_dir)
            recorder = ChangeRecorder()
            runtime = SessionRuntime(
                sid=sid,
                agent=Agent(
                    provider=self.provider,
                    registry=self._build_full_registry(recorder),
                    gate=gate,
                    working_dir=self.working_dir,
                    max_iterations=self.cfg.max_iterations,
                    context_limit_tokens=self._context_limit(),
                    compaction_keep_recent=self.cfg.compaction_keep_recent,
                    hooks=self.hooks,
                    restrict_to_workdir=self.cfg.restrict_to_workdir,
                ),
                recorder=recorder,
            )
            runtime.agent.set_system(self.compose_system())

            async def cron_emit(ev: dict) -> None:
                pass  # 无人值守：流式/权限/队列事件不进前端，结果走任务行

            result = await self._run_turn_pipeline(
                task["prompt"], cron_emit, plan_mode=False,
                images=None, runtime=runtime, session_id=sid,
            )
            # 取本轮最后一条助手文本作为结果摘要
            last_text = ""
            for m in reversed(runtime.agent.history):
                if m.role == "assistant" and m.text.strip():
                    last_text = m.text.strip().replace("\n", " ")[:200]
                    break
            status = "ok" if (not result.get("stopped") and last_text) else (
                "error" if result.get("stopped") else "empty")
            task = await self.store.update_cron_task(
                task_id,
                last_run_at=time.time(),
                last_status=status,
                last_result=last_text,
                enabled=task["enabled"],
            )
            # 下次运行时间：interval 基于本次完成时刻；daily/weekly 基于当前时刻
            task = await self.store.update_cron_task(
                task_id,
                next_run_at=self.store.compute_next_run({**task, "last_run_at": task["last_run_at"]}),
            )
            self._broadcast_cron(task)
            if status != "error":
                await self.notify({"title": f"⏰ 定时任务完成：{task['name']}",
                                   "body": last_text or "本轮没有产出"})
            else:
                await self.notify({"title": f"⏰ 定时任务异常：{task['name']}",
                                   "body": "本轮被中断或没有结果"})
        except Exception as e:  # noqa: BLE001 - 任务失败也要回写状态
            try:
                task = await self.store.update_cron_task(
                    task_id,
                    last_run_at=time.time(),
                    last_status="error",
                    last_result=str(e)[:200],
                )
                task = await self.store.update_cron_task(
                    task_id,
                    next_run_at=self.store.compute_next_run(
                        {**task, "last_run_at": task["last_run_at"]}
                    ) if task["enabled"] else 0,
                )
                self._broadcast_cron(task)
                await self.notify({"title": f"⏰ 定时任务失败：{task['name']}",
                                   "body": str(e)[:160]})
            except Exception:
                pass
        finally:
            self._cron_running.discard(task_id)

    # ---- 模型 ----

    def _build_provider(self, name: str, model: str | None = None) -> Provider:
        if self._provider_factory_override is not None:  # 测试/演示注入
            self.provider_name = name or "fake"
            p = self._provider_factory_override()
            self.provider_model = model or getattr(p, "model", "fake-1")
            return p
        if name not in self.cfg.providers:
            raise ConfigError(
                "unknown provider: {}; available: {}".format(name, ", ".join(self.cfg.providers))
            )
        pc = self.cfg.providers[name]
        if model:
            pc = pc.model_copy(update={"model": model})
        # 先构建成功再记录状态：构建失败（如缺 API Key）时不能改动"当前使用的模型"，
        # 否则 provider_name 指向新名字、provider 仍是旧对象，界面与实际调用会不一致。
        try:
            provider = build_provider(name, pc)
        except ConfigError as e:
            raise RuntimeError(str(e) + "\nhint: skysheep config init 后填入 API Key") from e
        self.provider_name = name
        self.provider_model = pc.model
        return provider

    # ---- 系统提示词 / 会话 ----

    def compose_system(self) -> str:
        return (
            build_system_prompt(self.working_dir)
            + self.skills.render_prompt_section()
            + render_instructions_section(self.instructions_file, self.instructions_text)
            + render_memory_section()
        )

    async def new_session(self) -> dict:
        self.session = await self.store.create_session(self.project.id)
        self._get_runtime(self.session.id)  # 预建 runtime（自带系统提示词）
        return {"id": self.session.id, "title": "", "summary": ""}

    async def open_initial_session(self) -> dict | None:
        """启动时接着上次的会话继续；完全没历史则不创建（懒创建：发第一条消息时才落库），
        避免每次启动都堆积空会话。"""
        latest = await self.store.latest_session(self.project.id)
        if latest is not None:
            return await self.resume_session(latest.id)
        self.session = None
        return None

    async def cleanup_empty_sessions(self) -> dict:
        """删除本项目下没有任何消息的空会话（保留当前会话与置顶会话）。"""
        keep = self.session.id if self.session else None
        removed = await self.store.delete_empty_sessions(self.project.id, keep_id=keep)
        return {"removed": removed}

    async def truncate_session(self, params: dict) -> dict:
        """消息级回退：为「重新生成 / 编辑重发」截断历史。

        mode=regen  ：删掉锚点（默认最后一条 user）之后的所有消息，保留用户消息；
        mode=edit   ：连锚点消息一起删（随后用户编辑后重发）。
        会话正在运行时拒绝（避免与进行中的 turn 互相踩踏）。
        """
        sid = str(params.get("id", "") or (self.session.id if self.session else ""))
        if not sid:
            raise RuntimeError("missing session id")
        rt = self.runtimes.get(sid)
        if rt and rt.run_task and not rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再操作")
        sess = await self.store.get_session(sid)
        if sess is None:
            raise RuntimeError("session not found: " + sid)

        seq = params.get("seq")
        mode = str(params.get("mode", "regen"))
        if seq is not None:
            pivot = int(seq)
        else:
            pivot = await self.store.find_last_user_seq(sid)
            if pivot is None:
                raise RuntimeError("会话里没有可回退的用户消息")
        # 校验锚点是 user 消息（regen 的语义是「重跑这条用户消息」）
        anchor = await self.store.get_message_at(sid, pivot)
        if anchor is None:
            raise RuntimeError("锚点消息不存在")
        if mode == "regen" and anchor["role"] != "user":
            # 自动回退到它前面最近的 user 消息
            rows = await self.store.search_messages_by_seq(sid, pivot, role="user")
            if rows is None:
                raise RuntimeError("锚点之前没有用户消息")
            pivot = rows
            anchor = await self.store.get_message_at(sid, pivot)

        include = mode == "edit"
        deleted = await self.store.truncate_from(sid, pivot, include_self=include)
        # 同步 runtime 内的历史（存在则从存储重载）
        if sid in self.runtimes:
            msgs = await self.store.load_messages(sid)
            self.runtimes[sid].agent.load_history(msgs or [Message.system(self.compose_system())])
        out = {"deleted": deleted, "pivot_seq": pivot, "mode": mode}
        if mode == "edit":
            out["text"] = anchor["text"]
        return out

    async def fork_session(self, params: dict) -> dict:
        """从某条消息分叉出新会话：复制 seq <= 锚点 的消息（缺省全部）。"""
        sid = str(params.get("id", "") or (self.session.id if self.session else ""))
        sess = await self.store.get_session(sid)
        if sess is None:
            raise RuntimeError("session not found: " + sid)
        seq = int(params["seq"]) if params.get("seq") is not None else (
            await self.store.max_seq(sid) or 0)
        new_sess = await self.store.create_session(
            self.project.id, title=(f"⑂ {sess.title or '分叉'}")[:40]
        )
        n = await self.store.copy_messages_between(sid, new_sess.id, seq)
        await self.store.touch(new_sess.id)
        msgs = await self.store.load_messages(new_sess.id)
        return {
            "id": new_sess.id,
            "title": new_sess.title,
            "copied": n,
            "messages": [_msg_brief(m) for m in msgs if m.role in ("user", "assistant")],
        }

    async def activate_session(self, session_id: str) -> dict:
        """只切活动指针，不重载历史（标签切换用；runtime 已存在时不做任何重活）。"""
        sess = await self.store.get_session(session_id)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        self.session = sess
        if session_id not in self.runtimes:
            rt = self._get_runtime(session_id)
            msgs = await self.store.load_messages(session_id)
            rt.agent.load_history(msgs or [Message.system(self.compose_system())])
        return {"id": sess.id, "title": sess.title}

    async def resume_session(self, session_id: str) -> dict:
        sess = await self.store.get_session(session_id)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        self.session = sess
        rt = self._get_runtime(session_id)
        msgs = await self.store.load_messages(session_id)
        rt.agent.load_history(msgs or [Message.system(self.compose_system())])
        return {
            "id": sess.id, "title": sess.title, "summary": sess.summary,
            "messages": [_msg_brief(m) for m in msgs if m.role in ("user", "assistant")],
        }

    # ---- 对话主流程 ----

    # 图片附件限制：类型白名单、单条最多 4 张、单张 base64 ≤ 6M 字符（约 4.5MB 原图）
    IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
    MAX_IMAGES_PER_TURN = 4
    MAX_IMAGE_B64_CHARS = 6_000_000

    def _sanitize_images(self, images) -> list[ImageBlock]:
        """清洗前端传来的图片附件：类型/大小/数量白名单，不合规静默丢弃。"""
        out: list[ImageBlock] = []
        for im in (images or [])[: self.MAX_IMAGES_PER_TURN]:
            if not isinstance(im, dict):
                continue
            mt = str(im.get("media_type", ""))
            data = str(im.get("data", ""))
            if mt not in self.IMAGE_MEDIA_TYPES or not data or len(data) > self.MAX_IMAGE_B64_CHARS:
                continue
            out.append(ImageBlock(media_type=mt, data=data))
        return out

    async def send(self, text: str, emit: EmitFn, plan_mode: bool = False,
                   roundtable: bool = False, members: list | None = None,
                   images: list[dict] | None = None,
                   session_id: str | None = None,
                   wants_title: bool = False,
                   regenerate: bool = False,
                   compare: bool = False) -> dict:
        """跑一轮对话；过程事件通过 emit 推送；结束后持久化新消息。

        Agent 正在工作时再次 send 不再报错，而是**排队**：等当前轮结束后
        自动依次执行（对标 Claude Code 的消息队列），fut 在该轮真正跑完时
        才返回结果。

        plan_mode=True 时本轮切换为只读工具集并附加规划指令（对标 Claude Code
        计划模式）：Agent 只做只读调研并输出实施计划，不改动任何文件。

        roundtable=True 时本轮走「圆桌」流程：多个模型并行独立作答，
        主席（当前主模型）融合成最终答案；members 为显式成员列表
        [{provider, model}]，缺省时自动选取（见 _resolve_members）。

        images 为用户消息携带的图片附件 [{media_type, data(base64)}]，
        只在普通轮生效（圆桌轮是纯文本协作，忽略图片）。

        session_id 指定目标会话（多会话并行时前端按标签传入）；缺省用活动会话。
        目标会话不是当前活动会话时先轻量激活（运行中的其他会话不受影响）。
        """
        if session_id and (not self.session or self.session.id != session_id):
            await self.activate_session(session_id)
        if self.provider is None:
            raise RuntimeError(
                "尚未配置可用的模型 API Key——"
                "请在 ~/.skysheep/config.toml 填入 api_key（或设置对应环境变量）后重启，"
                "或在左侧模型下拉中选择一个已配置 Key 的 provider。"
            )
        # 每日 token 预算护栏（设置 · 高级，默认 0 = 不限制）：超限即停，
        # 给可读的出路而不是让费用悄悄滚大。演示模式不受限（不产生真实费用）。
        if self.cfg.daily_token_budget > 0 and not getattr(self.provider, "demo_mode", False):
            used_today = await self.store.usage_today()
            if used_today >= self.cfg.daily_token_budget:
                raise RuntimeError(
                    f"已触发每日 token 预算护栏：今天已用约 {used_today:,} tokens，"
                    f"达到上限 {self.cfg.daily_token_budget:,}。\n"
                    "如需继续，请在 设置 · 高级 里调高或关闭「每日 token 预算」"
                    "（明天自动重置）。"
                )
        # 图片附件：当前服务声明"不支持图片输入"时提前给可读提示，
        # 而不是把图片塞给纯文本模型，换回一句上游报错（用户不知道是模型选错了）。
        clean_images = self._sanitize_images(images)
        if clean_images and not self._supports_vision():
            name = self.provider_name or "当前服务"
            raise RuntimeError(
                f"「{name} / {self.provider_model}」不支持图片输入，图片发不出去。\n"
                "请在输入框的模型选择器里换一个多模态模型（如 GLM-4V、Kimi、GPT-4o 等），"
                "或在 设置 · 模型服务 里把该服务的「支持图片输入」打开。"
            )
        # 在任何 await 之前先占住运行位：否则两条背靠背到达的消息会在
        # new_session() 的挂起点上双双判为空闲、并发执行（撞消息表唯一约束）。
        # 占位判断同时看基底位（会话懒创建窗口）与活动 runtime 位；后来者排队。
        cur = asyncio.current_task()
        rt_now = self.runtimes.get(self.session.id) if self.session else None
        holder = rt_now.run_task if rt_now else self._base_run_task
        if holder is not None and not holder.done() and holder is not cur:
            loop = asyncio.get_running_loop()
            item = QueuedTurn(
                text=text, emit=emit, plan_mode=plan_mode, fut=loop.create_future(),
                roundtable=roundtable, members=members,
                images=self._sanitize_images(images),
            )
            if rt_now is not None:
                rt_now.queue.append(item)
                await emit(QueueUpdated(pending=len(rt_now.queue)).model_dump())
            else:
                self._base_queue.append(item)
                await emit(QueueUpdated(pending=len(self._base_queue)).model_dump())
            return await item.fut
        self._base_run_task = cur  # 抢占基底位，掩护随后的懒创建 await 窗口
        if self.session is None:
            await self.new_session()  # 懒创建：第一条消息才落库
        runtime = self._get_runtime(self.session.id)
        runtime.run_task = cur
        self._base_run_task = None  # 运行位已落到 runtime，基底占位清除
        try:
            return await self._run_turn_pipeline(
                text, emit, plan_mode, roundtable=roundtable, members_params=members,
                images=clean_images, runtime=runtime,
                session_id=self.session.id, wants_title=wants_title,
                regenerate=regenerate, compare=compare,
            )
        except asyncio.CancelledError:
            # 用户在 turn 真正开始前点了停止：无产出，返回诚实的 stopped 结果，
            # 而不是让任务以异常结束（run_task 已经释放，后续消息不会进死队列）。
            if runtime.run_task is asyncio.current_task():
                runtime.run_task = None
            if self._base_run_task is asyncio.current_task():
                self._base_run_task = None
            return {
                "done": True,
                "stopped": True,
                "session_id": runtime.sid,
                "plan_mode": plan_mode,
                "roundtable": False,
                "context_tokens": runtime.agent.used_context_tokens(),
                "context_limit": runtime.agent.context_limit_tokens,
            }
        except BaseException:
            # pipeline 启动阶段就抛出（还没走到它自己的标志管理）时把位让出来，
            # 否则后续所有消息都会误入永远无人消费的队列。
            if runtime.run_task is asyncio.current_task():
                runtime.run_task = None
            if self._base_run_task is asyncio.current_task():
                self._base_run_task = None
            raise

    async def _auto_title(self, sid: str, first_user: str, first_reply: str) -> None:
        """首轮结束后用当前模型生成简短标题；失败保持原状，绝不影响主流程。"""
        if getattr(self.provider, "demo_mode", False):
            return  # 演示模式不额外消耗脚本组，标题保持首行截断即可
        if sid in self._titling:
            return
        self._titling.add(sid)
        try:
            cur_title = await self.store.get_session_title(sid)
            seed = f"用户：{first_user[:400]}"
            if first_reply:
                seed += f"\n助手：{first_reply[:300]}"
            prompt = (
                "给下面的对话起一个不超过12个字的中文标题，概括主题。"
                "只输出标题本身，不要引号、句号或任何解释。\n\n" + seed
            )
            parts: list[str] = []
            async for ev in self.provider.stream([Message.user(prompt)], []):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            raw = "".join(parts).strip()
            title = raw.splitlines()[0].strip(' \t"“”「」』【】。，；：')[:24] if raw else ""
            if not title or title == cur_title:
                return
            await self.store.set_title(sid, title)
            for ws_emit in list(self.ws_emitters):
                try:
                    await ws_emit({"kind": "session_updated",
                                   "session_id": sid, "title": title})
                except Exception:
                    pass
        except Exception:
            pass  # 标题生成失败完全静默
        finally:
            self._titling.discard(sid)

    async def _persist_turn(self, sid: str, new_msgs: list) -> None:
        """一轮对话的落库（独立方法便于 shield 保护：取消时后台完成落库）。"""
        for m in new_msgs:
            await self.store.append_message(sid, m)
        await self.store.touch(sid)
        summary = next(
            (m.text.strip().replace("\n", " ")[:120]
             for m in reversed(new_msgs) if m.role == "assistant" and m.text.strip()),
            "",
        )
        if summary:
            await self.store.set_summary(sid, summary)

    async def _run_turn_pipeline(
        self, text: str, emit: EmitFn, plan_mode: bool,
        roundtable: bool = False, members_params: list | None = None,
        images: list[ImageBlock] | None = None,
        runtime: SessionRuntime | None = None,
        session_id: str | None = None,
        wants_title: bool = False,
        regenerate: bool = False,
        compare: bool = False,
    ) -> dict:
        """真正执行一轮对话（含持久化、规划模式切换、检查点保存）。

        多会话并行：全程使用 runtime 内的 agent/queue/recorder，
        不碰 self.agent 等活动会话指针；事件发出时注入 session_id 供前端路由。
        """
        if runtime is None:
            if self.session is None:
                await self.new_session()  # 懒创建：第一条消息才落库
            runtime = self._get_runtime(self.session.id)
        sid = session_id or runtime.sid
        agent = runtime.agent

        async def emit_ev(ev: dict) -> None:
            await emit({**ev, "session_id": sid})

        sess_title = await self.store.get_session_title(sid)
        if not sess_title and not regenerate:
            sess_title = text[:40] or ("[图片]" if images else "")
            await self.store.set_title(sid, sess_title)

        readonly_registry = None
        if plan_mode:
            from ..tools.base import Safety

            readonly_registry = ToolRegistry(
                [t for t in agent.registry.all() if t.safety == Safety.READONLY]
            )
            agent.registry = readonly_registry
            text = PLAN_MODE_PREFIX + text

        n_before = len(agent.history)
        stopped = False
        rt_meta: dict | None = None
        tin0, tout0 = agent.total_in_tokens, agent.total_out_tokens
        runtime.run_task = asyncio.current_task()
        try:
            if roundtable:
                rt_meta = await self._roundtable_body(
                    text, emit_ev, members_params, agent=agent, compare=compare,
                )
            else:
                async for ev in agent.run_turn(text, images=images, append_user=not regenerate):
                    await emit_ev(ev.model_dump())
                    if ev.kind == "permission_request":
                        await self.store.touch(sid)
        except asyncio.CancelledError:
            stopped = True
        finally:
            if plan_mode and readonly_registry is not None:
                # 恢复完整工具集（只读注册表只在本轮生效）
                agent.registry = self._build_full_registry(runtime.recorder)

        # ---- 收尾必须严格串行：先落库，再交棒给排队轮，否则两条持久化
        # ---- 并发会撞 messages(session_id, seq) 唯一约束。
        # 取消保护：用户点停止时，已产出的消息仍要落库、运行位必须释放；
        # shield 让落库在后台继续，CancelledError 被捕获后正常走完收尾返回 stopped 结果，
        # 避免 send() 抛异常导致 runtime.run_task 悬挂、后续消息误入死队列。
        new_msgs = agent.history[n_before:]
        try:
            await asyncio.shield(self._persist_turn(sid, new_msgs))
        except asyncio.CancelledError:
            stopped = True
            asyncio.current_task().uncancel()

        # 用量记录：本轮实际消耗的输入/输出 tokens
        try:
            await self.store.add_usage(
                sid, self.provider_name, self.provider_model,
                agent.total_in_tokens - tin0, agent.total_out_tokens - tout0,
            )
        except Exception:
            pass

        # 首轮对话：后台用模型生成简短标题（8-12 字），替换首行截断文本
        if wants_title and self.provider is not None and sid not in self._titling:
            first_user = text
            first_reply = next(
                (m.text.strip() for m in new_msgs if m.role == "assistant" and m.text.strip()), ""
            )
            if first_user:
                asyncio.get_running_loop().create_task(
                    self._auto_title(sid, first_user, first_reply)
                )

        # 本轮改动了文件 → 存检查点（落盘 + 内存索引）；结果里带给前端做「撤销本轮改动」
        checkpoint = await asyncio.to_thread(
            self.checkpoints.save, sid, dict(runtime.recorder.pre)
        )
        runtime.recorder.reset()

        def _pop_next() -> bool:
            """启动下一个排队轮，把"运行中"的接力棒递给它；有交棒返回 True。"""
            if self._base_queue:
                # 会话懒创建期间排进基底队列的消息，并入本会话队列
                runtime.queue.extend(self._base_queue)
                self._base_queue.clear()
            if not runtime.queue:
                return False
            nxt = runtime.queue.pop(0)
            runtime.run_task = asyncio.get_running_loop().create_task(
                self._run_queued(nxt, runtime)
            )
            return True

        # queue_updated 是前端运行状态的唯一事实源：
        # pending = 仍排队轮数 +（1 如果刚接棒了一轮），归零才真正空闲
        handed_off = _pop_next()
        try:
            await emit_ev(
                QueueUpdated(pending=len(runtime.queue) + (1 if handed_off else 0)).model_dump()
            )
        except Exception:  # noqa: BLE001 - 客户端断开不影响收尾
            pass

        result = {
            "done": True,
            "stopped": stopped,
            "session_id": sid,
            "plan_mode": plan_mode,
            "roundtable": rt_meta,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
        }
        if checkpoint is not None:
            result["checkpoint"] = checkpoint
        # 没有交棒（无排队轮）才释放运行标志
        if runtime.run_task is asyncio.current_task():
            runtime.run_task = None
        # 兜底：交棒之后、释放标志前后又进来了排队请求 → 这里再拉起一次并广播
        if runtime.queue and (runtime.run_task is None or runtime.run_task.done()):
            handed_off2 = _pop_next()
            try:
                await emit_ev(
                    QueueUpdated(
                        pending=len(runtime.queue) + (1 if handed_off2 else 0)
                    ).model_dump()
                )
            except Exception:  # noqa: BLE001
                pass
        return result

    async def _run_queued(self, item: QueuedTurn, runtime: SessionRuntime) -> None:
        # 状态由 _run_turn_pipeline finally 里的 queue_updated 统一播报，这里不重复发
        try:
            item.resolve(await self._run_turn_pipeline(
                item.text, item.emit, item.plan_mode,
                roundtable=item.roundtable, members_params=item.members,
                images=item.images, runtime=runtime,
            ))
        except Exception as e:  # noqa: BLE001 - 错误要送回等待中的请求
            item.fail(e)

    def cancel_run(self, session_id: str | None = None) -> bool:
        """停止指定会话（缺省 = 活动会话）正在跑的 turn。"""
        rt = self.runtimes.get(session_id) if session_id else None
        task = (rt.run_task if rt else None) or (self._run_task if not session_id else None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    # ---- 圆桌：多模型并行独立作答 + 主席融合 ----

    def _build_member_provider(self, name: str, model: str | None) -> Provider:
        """为圆桌成员构建独立 Provider 实例（无副作用：不改当前使用的模型状态）。"""
        if self._provider_factory_override is not None:  # 测试/演示注入
            return self._provider_factory_override()
        if name not in self.cfg.providers:
            raise ConfigError(
                "unknown provider: {}; available: {}".format(name, ", ".join(self.cfg.providers))
            )
        pc = self.cfg.providers[name]
        if model:
            pc = pc.model_copy(update={"model": model})
        return build_provider(name, pc)

    def _resolve_members(self, members_params: list | None) -> list[MemberSpec]:
        """解析圆桌成员：显式列表优先；缺省时取所有已配置 Key 的服务的当前模型。

        规则：
        - 去重（同名同模型只留一个）；上限 cfg.roundtable.max_members（不含主席）；
        - 主席（当前主模型）默认也已作为成员出草稿，与它重复的成员跳过；
        - 单个成员构建失败（缺 Key/未知服务）不阻断，作答时以错误卡片呈现。
        """
        specs: list[MemberSpec] = []
        seen: set[tuple[str, str]] = set()
        limit = self.cfg.roundtable.max_members
        chair_key = (self.provider_name, self.provider_model)

        def _add(name: str, model: str) -> None:
            key = (name, model)
            if key in seen or len(seen) >= limit:
                return
            seen.add(key)
            try:
                provider = self._build_member_provider(name, model)
                specs.append(MemberSpec(
                    provider_name=name,
                    model=model or getattr(provider, "model", ""),
                    provider=provider,
                ))
            except Exception as e:  # noqa: BLE001 - 构建失败降级为错误成员卡片
                specs.append(MemberSpec(
                    provider_name=name, model=model, build_error=str(e)[:200]
                ))

        if members_params:
            for m in members_params:
                name = str((m or {}).get("provider", "")).strip()
                if not name:
                    continue
                model = str((m or {}).get("model", "") or "").strip()
                if self.cfg.roundtable.chair_answers and (name, model) == chair_key:
                    continue
                _add(name, model)
        else:
            for name, pc in self.cfg.providers.items():
                if resolve_api_key(name, pc) is None:
                    continue
                if self.cfg.roundtable.chair_answers and (name, pc.model) == chair_key:
                    continue
                _add(name, pc.model)
        return specs

    async def _roundtable_body(
        self, text: str, emit: EmitFn, members_params: list | None,
        agent: Agent | None = None, compare: bool = False,
    ) -> dict:
        """圆桌轮主体：成员并行作答 → 主席融合 → 结果并入主历史。

        返回随轮次结果回传前端的圆桌元数据；历史只追加
        user(问题) + assistant(融合答案)，成员草稿不入主历史。
        agent 缺省时用活动会话的（单会话调用路径兼容）。
        """
        if agent is None:
            agent = self.agent
        if self.provider is None:
            raise RuntimeError("圆桌需要当前主模型可用；请先在模型下拉中选择一个已配置 Key 的服务")

        async def emit_ev(ev) -> None:
            await emit(ev.model_dump())

        await emit_ev(TurnStarted(iteration=1))
        members = self._resolve_members(members_params)
        if not members:
            await emit_ev(TurnFinished(stop_reason="error", iterations=1))
            raise RuntimeError(
                "圆桌没有可用成员：请先在成员面板选择，或给更多模型服务配置 API Key"
            )
        if self.cfg.roundtable.chair_answers:
            members.insert(0, MemberSpec(
                provider_name=self.provider_name,
                model=self.provider_model,
                provider=self.provider,
            ))

        system = history_system_text(agent.history) or self.compose_system()
        outcome: RoundtableOutcome = await run_roundtable(
            members=members,
            chair=self.provider,
            system_text=system,
            history=agent.history,
            user_text=text,
            timeout_s=self.cfg.roundtable.member_timeout_s,
            emit=emit_ev,
        )

        agent.history.append(Message.user(text))
        meta = {
            "mode": "roundtable",
            "chair": {"provider": self.provider_name, "model": self.provider_model},
            "chair_answers": self.cfg.roundtable.chair_answers,
            "members": [
                {
                    "provider": r.spec.provider_name,
                    "model": r.spec.model,
                    "status": r.status,
                    "error": r.error,
                    "output_tokens": r.output_tokens,
                }
                for r in outcome.members
            ],
        }
        if compare:
            # A/B 对比：每个成员的回答单独成一条消息（带归属徽标），不融合
            meta["mode"] = "compare"
            kept = 0
            for r in outcome.members:
                if r.status != "done" or not r.text.strip():
                    continue
                m = Message.assistant([TextBlock(text=r.text.strip())])
                m.roundtable = {
                    "mode": "compare",
                    "chair": meta["chair"],
                    "members": [{
                        "provider": r.spec.provider_name, "model": r.spec.model,
                        "status": r.status, "error": r.error,
                        "output_tokens": r.output_tokens,
                    }],
                }
                agent.history.append(m)
                await emit_ev(AssistantMessage(message=m.model_dump()))
                kept += 1
            meta["compared"] = kept
            await emit_ev(TurnFinished(
                stop_reason="end_turn" if kept else "error", iterations=1
            ))
            return meta
        if outcome.fused_text:
            assistant = Message.assistant([TextBlock(text=outcome.fused_text)])
            assistant.roundtable = meta
            agent.history.append(assistant)
            await emit_ev(AssistantMessage(message=assistant.model_dump()))
        else:
            await emit_ev(ErrorEvent(
                message=f"圆桌融合失败：{outcome.error or '所有成员均未产出回答'}"
            ))
            meta["status"] = "error"
            meta["error"] = outcome.error
        await emit_ev(TurnFinished(
            stop_reason="end_turn" if outcome.fused_text else "error", iterations=1
        ))
        return meta

    # ---- Slash 命令支撑（/compact /status /todos，前端输入 / 唤出菜单） ----

    async def compact_now(self) -> dict:
        """手动触发上下文压缩（/compact）。没有可压缩内容时返回 compacted=False。"""
        agent = self.agent
        ev = await compact_history(agent, keep_recent=self.cfg.compaction_keep_recent)
        return {
            "compacted": ev is not None,
            "before": ev.before_messages if ev else 0,
            "after": ev.after_messages if ev else 0,
            "summary_chars": ev.summary_chars if ev else 0,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
        }

    def status(self) -> dict:
        """/status：模型、上下文占用、工具数、任务清单一览。"""
        todo_tool = self.agent.registry.get("todo_write")
        return {
            "version": __version__,
            "working_dir": str(self.working_dir),
            "provider": self.provider_name,
            "model": self.provider_model,
            "provider_error": self.provider_error,
            "session_id": self.session.id if self.session else None,
            "context_tokens": self.agent.used_context_tokens(),
            "context_limit": self.agent.context_limit_tokens,
            "history_messages": len(self.agent.history),
            "tool_count": len(self.agent.registry),
            "queued": len(self.queue),
            "todos": list(getattr(todo_tool, "items", []) or []),
        }

    async def tasks_list(self) -> dict:
        return {"tasks": self.tasks.list_tasks() if self.tasks else []}

    async def tasks_cancel_all(self) -> dict:
        if self.tasks:
            self.tasks.cancel_all()
        return {"cancelled": True}

    async def usage_stats(self, params: dict) -> dict:
        """用量统计：按天/会话/服务聚合 + 按 provider 单价估算费用。"""
        days = min(90, max(1, int(params.get("days", 14))))
        st = await self.store.usage_stats(days)
        prices = {
            name: {"price_in": pc.price_in, "price_out": pc.price_out}
            for name, pc in self.cfg.providers.items()
        }
        cost = 0.0
        for row in st["by_provider"]:
            pr = prices.get(row["provider"] or "", {})
            cost += (row["it"] or 0) / 1e6 * pr.get("price_in", 0.0)
            cost += (row["ot"] or 0) / 1e6 * pr.get("price_out", 0.0)
        total_in = sum(r["it"] or 0 for r in st["by_provider"])
        total_out = sum(r["ot"] or 0 for r in st["by_provider"])
        return {
            "days": days,
            "by_day": st["by_day"],
            "by_session": st["by_session"],
            "by_provider": st["by_provider"],
            "total_in": total_in,
            "total_out": total_out,
            "cost": round(cost, 4),
            "has_price": any(p["price_in"] or p["price_out"] for p in prices.values()),
        }

    async def fs_read(self, params: dict) -> dict:
        """只读预览工作区文件（文件树用）：文本直接读；PDF/Word/Excel 提取文本；
        其余二进制拒绝。沙箱限制在工作目录内。"""
        raw = str(params.get("path", "") or "").strip()
        if not raw:
            raise RuntimeError("missing path")
        target = Path(raw)
        if not target.is_absolute():
            target = self.working_dir / target
        try:
            target = target.resolve()
            target.relative_to(self.working_dir)
        except (OSError, ValueError):
            raise RuntimeError("只能预览工作目录内的文件") from None
        if not target.is_file():
            raise RuntimeError("文件不存在（或这是目录）: " + raw)
        try:
            size = target.stat().st_size
        except OSError as e:
            raise RuntimeError("读取失败: " + str(e)) from None

        # 文档类文件：提取文本预览（与 read_document 工具同一套解析器）
        if target.suffix.lower() in (".pdf", ".docx", ".xlsx"):
            if size > 50_000_000:
                raise RuntimeError(f"文件太大（{size / 1048576:.0f}MB），上限 50MB")
            from ..tools.docs import extract_document_text

            try:
                text = await asyncio.to_thread(extract_document_text, target, 60_000)
            except Exception as e:  # noqa: BLE001 - ToolError/解析错误统一转可读提示
                raise RuntimeError(str(e)) from None
            return {
                "path": str(target.relative_to(self.working_dir)).replace("\\", "/"),
                "text": text,
                "size": size,
                "truncated": False,
                "doc": True,
            }

        try:
            data = target.read_bytes()[:400_000]
        except OSError as e:
            raise RuntimeError("读取失败: " + str(e)) from None
        if b"\x00" in data:
            raise RuntimeError("二进制文件不支持预览")
        text = data.decode("utf-8", errors="replace")
        return {
            "path": str(target.relative_to(self.working_dir)).replace("\\", "/"),
            "text": text,
            "size": size,
            "truncated": size > 400_000,
        }

    async def workspace_files(self) -> dict:
        """/@ 文件提及的数据源：项目内文件相对路径清单（跳过依赖与构建目录）。"""
        return await asyncio.to_thread(self._walk_workspace_files)

    SKIP_DIRS = {
        ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", "dist", "build", ".skysheep", ".mimosa",
        ".next", "target", ".idea", ".vscode", ".preview", ".zcode", ".agents",
    }
    MAX_FILES = 400

    def _walk_workspace_files(self) -> dict:
        from ..core.ignore import IgnoreRules

        out: list[str] = []
        dirs: list[str] = []
        root = self.working_dir
        ignore = IgnoreRules.load(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in self.SKIP_DIRS and not d.startswith(".git")
                and not ignore.matches((Path(dirpath) / d).relative_to(root).as_posix(), is_dir=True)
            ]
            rel_base = Path(dirpath).relative_to(root)
            if rel_base != Path("."):
                # 目录也进索引（@补全里以 / 结尾）；只收录直接子层，避免清单爆炸
                dirs.append(rel_base.as_posix() + "/")
            for fn in filenames:
                rel = (rel_base / fn).as_posix()
                if ignore.matches(rel):
                    continue
                if len(out) >= self.MAX_FILES:
                    return {"files": sorted(out), "root": str(root), "truncated": True}
                out.append(rel)
        # 目录排在文件前（@菜单分组展示），各自排序
        return {"files": sorted(dirs)[: self.MAX_FILES // 4] + sorted(out),
                "root": str(root), "truncated": False}

    # ---- 检查点（撤销本轮改动） ----

    async def list_checkpoints(self) -> dict:
        sid = self.session.id if self.session else ""
        return {"checkpoints": self.checkpoints.list_for(sid)}

    async def restore_checkpoint(self, checkpoint_id: str) -> dict:
        """把某轮的文件改动回滚到改前状态；并告知模型「文件已被回滚」。"""
        try:
            files = self.checkpoints.restore(checkpoint_id)
        except KeyError:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（保留最近 50 轮，更早的会被淘汰）"
            ) from None
        note = Message.user(
            "(系统提示) 用户执行了「撤销本轮改动」，以下文件已恢复到本轮改动前的状态："
            + ", ".join(files)
            + "。后续如需引用这些文件请先重新读取。"
        )
        cp = self.checkpoints.get(checkpoint_id)
        sid = (cp or {}).get("session_id") or (self.session.id if self.session else None)
        agent = self.runtimes[sid].agent if sid and sid in self.runtimes else self.agent
        agent.history.append(note)
        if sid:
            await self.store.append_message(sid, note)
        return {"restored": checkpoint_id, "files": files}

    async def checkpoint_diff(self, checkpoint_id: str) -> dict:
        """「审查」标签页：某个检查点里每个文件的 改前快照 vs 磁盘现状 的 unified diff。"""
        cp = self.checkpoints.get(checkpoint_id)
        if cp is None:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（保留最近 50 轮，更早的会被淘汰）"
            )
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
        return {"id": checkpoint_id, "ts": cp["ts"], "files": files}

    # ---- 右侧面板：终端 / 辅助对话 ----

    async def term_run(self, command: str, emit: EmitFn) -> dict:
        command = (command or "").strip()
        if not command:
            raise RuntimeError("命令不能为空")
        return await self.term.run(command, self.working_dir, emit)

    def term_stop(self) -> dict:
        return {"stopped": self.term.stop()}

    AUX_SYSTEM = (
        "你是 SkySheep 侧边面板中的辅助助手，负责回答主对话之外的快速小问题。"
        "保持简短、直接、可操作，不调用任何工具。当前工作目录：{cwd}"
    )
    AUX_HISTORY_CAP = 31  # system + 15 轮问答

    async def chat_aux(self, text: str, emit: EmitFn) -> dict:
        """辅助对话：独立于主会话的轻量一问一答（不落库、不带工具、内存历史）。"""
        text = (text or "").strip()
        if not text:
            raise RuntimeError("empty text")
        if self.provider is None:
            detail = self.provider_error or "请先在 设置 · 模型服务 里启用一个模型"
            raise RuntimeError(f"模型服务未配置或不可用：{detail}")
        if not self.aux_history:
            self.aux_history.append(Message.system(self.AUX_SYSTEM.format(cwd=self.working_dir)))
        self.aux_history.append(Message.user(text))
        parts: list[str] = []
        think_parts: list[str] = []
        try:
            async for pe in self.provider.stream(list(self.aux_history), []):
                if isinstance(pe, ProviderTextDelta):
                    parts.append(pe.text)
                    await emit({"kind": "aux_delta", "text": pe.text})
                elif isinstance(pe, ProviderReasoning):
                    # 思考模型的推理增量：面板实时灰显，不进历史与回复
                    think_parts.append(pe.text)
                    await emit({"kind": "aux_thinking", "text": pe.text})
                elif isinstance(pe, ProviderDone):
                    pass  # 辅助对话不计入用量统计
        except Exception:
            # 失败的这轮不入历史，避免污染后续上下文
            self.aux_history.pop()
            raise
        reply = "".join(parts)
        self.aux_history.append(Message.assistant([TextBlock(text=reply)]))
        overflow = len(self.aux_history) - self.AUX_HISTORY_CAP
        if overflow > 0:
            del self.aux_history[1 : 1 + overflow]  # system 永远保留在首位
        return {"text": reply}

    def aux_clear(self) -> dict:
        self.aux_history = []
        return {"cleared": True}

    def respond_permission(self, request_id: str, decision: str) -> bool:
        for ag in self._for_each_agent():
            if ag.respond_permission(request_id, decision):
                return True
        return False

    # ---- 分级权限模式（对标 Codex Auto-Edit / Claude Code acceptEdits） ----

    def permission_mode(self) -> str:
        return "accept_edits" if (self.gate and self.gate.auto_accept_write) else "confirm"

    async def set_permission_mode(self, mode: str) -> dict:
        """confirm = 写入/命令都确认（默认）；accept_edits = 写入自动放行、命令仍确认。"""
        if mode not in ("confirm", "accept_edits"):
            raise RuntimeError("权限模式只支持 confirm / accept_edits")
        if self.gate is None:
            raise RuntimeError("引擎尚未就绪")
        self.gate.auto_accept_write = mode == "accept_edits"
        await self.save_ui_prefs({"accept_edits": 1 if mode == "accept_edits" else 0})
        return {"mode": self.permission_mode()}

    # ---- 模型/配置操作 ----

    async def switch_model(self, name: str, model: str | None = None) -> dict:
        self.provider = self._build_provider(name, model)
        self.provider_error = None
        for ag in self._for_each_agent():
            ag.provider = self.provider
        # 换服务/换模型 = 换上下文窗口：上限与视觉能力都跟着新的服务走
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.context_limit_tokens = limit
        return {
            "provider": name, "model": self.provider_model,
            "reasoning": self.reasoning_state(),
            "context_limit": limit,
            "supports_vision": self._supports_vision(),
        }

    def reasoning_state(self) -> dict:
        """当前思考强度状态：是否支持 + 当前档位 + 各服务自己的档位。"""
        cur = getattr(self.provider, "reasoning_effort", "auto")
        return {
            "supported": bool(getattr(self.provider, "supports_reasoning", False)),
            "effort": cur,
            "efforts": list(REASONING_EFFORTS),
            "labels": dict(REASONING_EFFORT_LABELS),
            "providers": {
                name: {
                    "supports_reasoning": bool(pc.supports_reasoning),
                    "effort": pc.reasoning_effort,
                }
                for name, pc in self.cfg.providers.items()
            },
        }

    async def set_reasoning_effort(self, effort: str, name: str | None = None) -> dict:
        """调整思考强度并热生效。

        name 缺省 = 当前使用的服务；写入 config.toml 的对应 provider 后重建当前
        provider，让下一轮对话立即用新档位。不支持该能力的服务直接报错。
        """
        target = name or self.provider_name
        effort = str(effort or "auto").strip().lower()
        if effort not in REASONING_EFFORTS:
            raise RuntimeError("思考强度只支持 " + " / ".join(REASONING_EFFORTS))
        pc = self.cfg.providers.get(target)
        # 当前 provider（含测试注入/自定义装配）即使不在配置表里也允许直接调档
        is_current = bool(self.provider is not None and target == self.provider_name)
        if pc is None and not is_current:
            raise RuntimeError("unknown provider: " + str(target))
        supports = bool(pc.supports_reasoning) if pc is not None else bool(
            getattr(self.provider, "supports_reasoning", False)
        )
        if not supports:
            raise RuntimeError(f"「{target}」不支持调整思考强度（未声明该能力）")
        if pc is not None:
            update_provider_in_config(target, reasoning_effort=effort)
            self.cfg = load_config()
        if is_current and self.provider is not None:
            # 不重建整个 provider（避免丢 Key/连接），直接改档位即可生效
            self.provider.set_reasoning_effort(effort)
            for ag in self._for_each_agent():
                ag.provider = self.provider
        return {"provider": target, "reasoning": self.reasoning_state()}

    async def add_provider_model(self, name: str, model: str) -> dict:
        """把一个模型加入该服务的已启用列表（不动当前使用的模型）。"""
        model = (model or "").strip()
        if not model:
            raise RuntimeError("模型 ID 不能为空")
        pc = self.cfg.providers.get(name)
        if pc is None:
            raise RuntimeError("unknown provider: " + name)
        models = list(pc.models)
        if model in models:
            return {"name": name, "models": models, "added": False}
        models.append(model)
        set_provider_models_in_config(name, models)
        self.cfg = load_config()
        # 不变量：当前使用的模型必须 ∈ 已启用列表。比如预设自带的占位模型
        # 不在拉取到的列表里时，就把新加的设为当前模型。
        current = self.provider_model if name == self.provider_name else self.cfg.providers[name].model
        activated = False
        if current not in models:
            update_provider_in_config(name, model=model)
            self.cfg = load_config()
            if name == self.provider_name:
                try:
                    await self.switch_model(name, model)
                    activated = True
                except RuntimeError:
                    pass
        return {"name": name, "models": models, "added": True, "activated": activated}

    async def remove_provider_model(self, name: str, model: str) -> dict:
        """从已启用列表里删除一个模型；列表至少要留一个。"""
        pc = self.cfg.providers.get(name)
        if pc is None:
            raise RuntimeError("unknown provider: " + name)
        models = list(pc.models)
        if model not in models:
            raise RuntimeError(f"「{model}」不在该服务的已启用列表里")
        if len(models) == 1:
            raise RuntimeError("至少保留一个启用的模型；先添加新的，再删除旧的")
        models.remove(model)
        set_provider_models_in_config(name, models)
        self.cfg = load_config()
        switched_to = None
        # 删的是该服务的当前模型 → 把它的当前模型换成剩下的第一个
        if self.cfg.providers[name].model == model:
            update_provider_in_config(name, model=models[0])
            self.cfg = load_config()
            if name == self.provider_name:
                await self.switch_model(name, models[0])
                switched_to = models[0]
        return {"name": name, "removed": model, "models": models, "switched_to": switched_to}

    async def toggle_skill(self, name: str, enabled: bool) -> dict:
        if not self.skills.set_enabled(name, enabled):
            raise RuntimeError("skill not found: " + name)
        self.skills.discover()
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        return {"name": name, "enabled": enabled}

    # ---- 技能：程序内导入 / 删除 ----

    def _skill_root(self, scope: str) -> Path:
        """技能安装位置：global = 所有项目共用，project = 只在当前项目生效。"""
        if scope == "project":
            return self.working_dir / ".skysheep" / "skills"
        if scope != "global":
            raise RuntimeError("scope 只能是 global 或 project: " + str(scope))
        return skysheep_home() / "skills"

    async def install_skill(self, source: str, scope: str = "global") -> dict:
        """把技能装进技能目录，装完立即生效（无需重启）。

        source 可以是本机文件夹、.zip 路径，或 GitHub / Gitee 的仓库链接与
        .zip 直链（网址下载放到工作线程，不卡事件循环）。
        """
        root = self._skill_root(scope)
        existing = {s.name for s in self.skills.all()}
        try:
            if str(source).strip().lower().startswith(("http://", "https://")):
                result = await asyncio.to_thread(install_from_url, source, root, existing=existing)
            else:
                result = await asyncio.to_thread(install_skill, source, root, existing=existing)
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        # 重新发现 + 重建系统提示词：新技能马上出现在清单里
        self.skills.discover()
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        result["scope"] = scope
        result["skills"] = [
            {"name": s.name, "description": s.description, "source": s.source, "enabled": s.enabled}
            for s in self.skills.all()
        ]
        return result

    async def delete_skill(self, name: str) -> dict:
        """删除技能目录。全局技能与项目技能都可能重名，按当前加载到的那一份删。"""
        skill = self.skills.get(name)
        if skill is None:
            raise RuntimeError("skill not found: " + name)
        scope = "project" if skill.source == "project" else "global"
        roots = [self.working_dir / ".skysheep" / "skills", skysheep_home() / "skills"]
        try:
            result = remove_skill(name, roots)
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        self.skills.discover()
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        result["scope"] = scope
        return result

    # ---- MCP：程序内导入 / 删除 / 重连 ----

    def _reload_mcp_configs(self) -> None:
        self.mcp_configs = load_mcp_configs(
            self._mcp_global_path(),
            self._mcp_project_path(),
        )

    def _mcp_global_path(self) -> Path:
        return mcp_config_path(skysheep_home())

    def _mcp_project_path(self) -> Path:
        return self.working_dir / ".skysheep" / "mcp.json"

    async def _reconnect_mcp(self) -> list[str]:
        """重建 MCP 工具集合并接到 Agent 上（改完配置后调用，无需重启）。"""
        if self.mcp is not None:
            await self.mcp.shutdown()
        self._reload_mcp_configs()
        self.mcp = MCPManager(self.mcp_configs)
        self.mcp_tools = await self.mcp.connect_all()
        self.mcp_warnings = [
            f"{name}: {st.error}" for name, st in self.mcp.statuses.items() if st.error
        ]
        self._apply_registry_to_agents()
        return self.mcp_warnings

    @staticmethod
    def _mcp_status_list(manager: MCPManager | None) -> list[dict]:
        if manager is None:
            return []
        return [
            {"name": n, "connected": st.connected, "error": st.error, "tools": st.tool_names}
            for n, st in manager.statuses.items()
        ]

    async def import_mcp_servers(
        self,
        *,
        snippet: str = "",
        path: str = "",
        scope: str = "global",
        overwrite: bool = False,
        reconnect: bool = True,
    ) -> dict:
        """导入 MCP 服务：从粘贴的 JSON（snippet）或一个本机 .json 文件（path）。"""
        if snippet.strip():
            servers = parse_snippet(snippet)
        elif path.strip():
            servers = parse_file(path)
        else:
            raise RuntimeError("请粘贴 MCP 配置，或指定一个 .json 文件路径")
        target = self._mcp_global_path() if scope == "global" else self._mcp_project_path()
        try:
            result = import_servers(servers, target, overwrite=overwrite)
        except MCPInstallError as e:
            raise RuntimeError(str(e)) from e

        if reconnect and result["added"]:
            await self._reconnect_mcp()
        result["scope"] = scope
        result["mcp"] = self._mcp_status_list(self.mcp)
        result["mcp_warnings"] = self.mcp_warnings
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
        readonly: bool = False,
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
        if readonly:
            raw["readonly"] = True
        try:
            cfg = normalize_server(raw)
            target = self._mcp_global_path() if scope == "global" else self._mcp_project_path()
            result = import_servers({name.strip(): cfg}, target, overwrite=overwrite)
        except MCPInstallError as e:
            raise RuntimeError(str(e)) from e
        if result["added"]:
            await self._reconnect_mcp()
        result["scope"] = scope
        result["mcp"] = self._mcp_status_list(self.mcp)
        result["mcp_warnings"] = self.mcp_warnings
        return result

    async def add_mcp_preset(self, name: str, scope: str = "global") -> dict:
        """一键添加内置预设 MCP 服务：按预设原文写入 mcp.json 并立即连接。

        args 里的 {dir} 占位符替换为当前工作目录；同名已存在时不覆盖
        （save_mcp_server 用 overwrite=False，走 skipped 通道）。
        """
        preset = preset_by_name(name)
        if preset is None:
            raise RuntimeError(f"没有这个内置预设：{name}")
        args = [str(a).replace("{dir}", str(self.working_dir)) for a in preset["args"]]
        result = await self.save_mcp_server(
            preset["name"],
            command=preset["command"],
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
        path = self._mcp_global_path() if scope == "global" else self._mcp_project_path()
        try:
            result = remove_server(name, path)
        except MCPInstallError as e:
            # 全局/项目两边都试试，用户不必知道它当初存在哪
            other = self._mcp_project_path() if scope == "global" else self._mcp_global_path()
            try:
                result = remove_server(name, other)
                path = other
            except MCPInstallError:
                raise RuntimeError(str(e)) from e
        await self._reconnect_mcp()
        result["scope"] = "project" if path == self._mcp_project_path() else "global"
        result["mcp"] = self._mcp_status_list(self.mcp)
        return result

    # ---- 设置页 ----

    # 子代理设置：读当前值 / 保存并热生效

    def subagent_settings(self) -> dict:
        return {
            "enabled": self.cfg.subagent_enabled,
            "max_iterations": self.cfg.subagent_max_iterations,
            "main_max_iterations": self.cfg.max_iterations,
        }

    async def save_subagent_settings(
        self, *, enabled: bool | None = None, max_iterations: int | None = None
    ) -> dict:
        """保存子代理设置并立即生效：开关控注册表、轮数热更新任务簿，不用重启。"""
        if max_iterations is not None:
            try:
                max_iterations = int(max_iterations)
            except (TypeError, ValueError):
                raise RuntimeError("迭代轮数要是 1–100 之间的整数") from None
            if not 1 <= max_iterations <= 100:
                raise RuntimeError("迭代轮数要在 1–100 之间")
        try:
            set_subagent_settings_in_config(enabled=enabled, max_iterations=max_iterations)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        self.tasks.set_max_iterations(self.cfg.subagent_max_iterations)
        if enabled is not None:
            self._apply_registry_to_agents()
        return self.subagent_settings()

    # ---- 高级设置（主循环轮数 / 上下文上限 / 压缩保留 / 目录限制 / 开机自启） ----

    def _apply_advanced_to_agents(self) -> None:
        """把改动推给所有活着的 Agent，不用重启应用。"""
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.max_iterations = self.cfg.max_iterations
            ag.context_limit_tokens = limit
            ag.compaction_keep_recent = self.cfg.compaction_keep_recent
            ag.restrict_to_workdir = self.cfg.restrict_to_workdir

    def _startup_status(self) -> dict:
        from .. import startup

        try:
            return startup.status()
        except Exception as e:  # noqa: BLE001 - 注册表异常不该让设置页打不开
            return {"supported": startup.is_supported(), "enabled": False,
                    "command": "", "current": "", "stale": False, "error": str(e)}

    def advanced_settings(self) -> dict:
        return {
            "max_iterations": self.cfg.max_iterations,
            "context_limit_tokens": self.cfg.context_limit_tokens,
            "compaction_keep_recent": self.cfg.compaction_keep_recent,
            "restrict_to_workdir": self.cfg.restrict_to_workdir,
            "computer_control": self.cfg.computer_control,
            "browser_control": self.cfg.browser_control,
            "daily_token_budget": self.cfg.daily_token_budget,
            "context_limit_tokens_effective": self._context_limit(),
            "current_provider": self.provider_name,
            "current_provider_context_limit": (
                self._current_provider_cfg().context_limit if self._current_provider_cfg() else 0
            ),
            "home": str(skysheep_home()),
            "logs_dir": str(skysheep_home() / "logs"),
            "working_dir": str(self.working_dir),
            "autostart": self._startup_status(),
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
        restrict = params.get("restrict_to_workdir")
        budget = params.get("daily_token_budget")
        if budget is not None and budget != "":
            try:
                budget = int(budget)
            except (TypeError, ValueError):
                raise RuntimeError("每日 token 预算需要一个整数") from None
        else:
            budget = None
        try:
            set_advanced_settings_in_config(
                max_iterations=ints.get("max_iterations"),
                context_limit_tokens=ints.get("context_limit_tokens"),
                compaction_keep_recent=ints.get("compaction_keep_recent"),
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
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        self._apply_advanced_to_agents()
        # 电脑/浏览器控制开关改变了工具清单：重建基础 Agent 与所有会话运行时的注册表
        # （与 MCP 热生效同一套机制；运行中的轮次下一次调用时自然使用新清单）
        self._apply_registry_to_agents()
        self.tasks.set_restrict_to_workdir(self.cfg.restrict_to_workdir)

        autostart = params.get("autostart")
        if autostart is not None:
            from .. import startup

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
            "workdir": Path(self.working_dir),
        }
        target = mapping.get(str(kind or ""))
        if target is None:
            raise RuntimeError("未知的目录类型：" + str(kind))
        return target

    def open_path(self, kind: str) -> dict:
        """用系统文件管理器打开一个固定目录（日志/数据/工作目录）。"""
        from .. import support

        target = self._home_subdir(kind)
        try:
            support.open_folder(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开目录失败：{e}") from None
        return {"path": str(target)}

    def open_external(self, target: str) -> dict:
        """用系统默认程序打开一个 http(s) 链接（反馈页/下载页/注册页）。

        只接受 http(s) URL；目标由前端界面写死或用户在向导里点选，
        不作为任意跳转接口。
        """
        from .. import support

        target = str(target or "").strip()
        if not target.startswith(("http://", "https://")):
            raise RuntimeError("只允许打开 http(s) 链接")
        try:
            support.open_external(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开链接失败：{e}") from None
        return {"url": target}

    # ---- 首启激活：演示模式 / 本地 Ollama 检测（无 Key 用户的两条出路） ----

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

    async def ollama_detect(self) -> dict:
        """探测本机 Ollama（localhost:11434）是否在运行、有哪些模型。

        首启向导用它把「装过 Ollama 的用户」从「必须去注册 API Key」的
        路径里解救出来——检测到即提示一键启用，零 Key 零费用。
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                resp = await client.get("http://localhost:11434/api/tags")
            resp.raise_for_status()
            models = [
                str(m.get("name") or "")
                for m in (resp.json().get("models") or [])
                if m.get("name")
            ]
        except Exception:  # noqa: BLE001 - 没装/没起/端口不通：都视为不可用
            return {"available": False, "models": []}
        return {"available": True, "models": models}

    async def ollama_enable(self, model: str = "") -> dict:
        """启用本地 Ollama：把检测到的模型写入 ollama 预设并切换过去。"""
        if not model:
            det = await self.ollama_detect()
            models = det.get("models") or []
            if not det.get("available") or not models:
                raise RuntimeError("没有检测到本机 Ollama（或其中没有任何模型）。"
                                   "请先安装并启动 Ollama，再回来点「检测」。")
            model = models[0]
        update_provider_in_config("ollama", model=model, set_default=True)
        self.cfg = load_config()
        await self.switch_model("ollama", model=model)
        return {"provider": "ollama", "model": model}

    def export_diagnostics(self) -> dict:
        """打包诊断 zip（版本/环境/日志/打码后的配置）并打开所在目录。"""
        from .. import support

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
        from .. import support

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

    # ---- 子代理：定义管理（独立设置页） ----

    def _subagent_provider(self, provider_name: str, model: str, reasoning: str):
        """按子代理定义解析出 Provider。

        provider 留空 = 跟随主对话当前模型（用临时实例，不污染主 provider 的
        思考强度状态）；指定了服务就按 (服务, 模型, 思考强度) 现场构建。
        """
        provider_name = (provider_name or "").strip()
        model = (model or "").strip()
        reasoning = (reasoning or "").strip().lower()
        if provider_name and provider_name in self.cfg.providers:
            data = self.cfg.providers[provider_name].model_dump()
            if model:
                data["model"] = model
            if reasoning:
                data["reasoning_effort"] = reasoning
            return build_provider(provider_name, ProviderConfig(**data))
        if self.provider is None:
            raise RuntimeError("主对话当前没有可用模型，子代理无法启动——先到模型服务里配好 Key")
        if reasoning and self.provider_name and self.provider_name in self.cfg.providers:
            pc = self.cfg.providers[self.provider_name].model_copy(
                update={"reasoning_effort": reasoning}
            )
            return build_provider(self.provider_name, pc)
        return self.provider

    def _subagent_registry(self, policy):
        """把工具范围策略解析成子代理注册表。

        派生工具（spawn_agent/check_task）永远不进子代理——禁止递归派生；
        就算给了写工具，SubagentGate 也会自动拒绝需要确认的操作。
        """
        base = self._base_agent.registry
        exclude = {"spawn_agent", "check_task"}
        if isinstance(policy, str):
            policy = (policy or "").strip().lower() or "readonly"
            if policy == "all":
                tools = [t for t in base.all() if t.name not in exclude]
            else:
                tools = [
                    t for t in base.all()
                    if t.safety == Safety.READONLY and t.name not in exclude
                ]
        else:
            want = {str(n).strip() for n in (policy or []) if str(n).strip()}
            tools = [t for t in base.all() if t.name in want and t.name not in exclude]
        if not tools:
            raise RuntimeError("子代理工具集为空：至少要有一个可用工具")
        return ToolRegistry(tools)

    def subagents_detail(self) -> dict:
        """「子代理」设置页的一次性数据：全局设置 + 定义 + 可选项。"""
        providers = []
        for name, pc in self.cfg.providers.items():
            models = list(pc.models) or ([pc.model] if pc.model else [])
            providers.append({
                "name": name,
                "model": pc.model,
                "models": models,
                "has_key": resolve_api_key(name, pc) is not None,
            })
        tools = [
            {"name": t.name, "safety": t.safety.value, "description": t.description}
            for t in self._base_agent.registry.all()
            if t.name not in ("spawn_agent", "check_task")
        ]
        return {
            **self.subagent_settings(),
            "builtin": {t: ov.model_dump() for t, ov in self.subagent_store.builtin.items()},
            "builtin_display": dict(BUILTIN_DISPLAY),
            "custom": [d.model_dump() for d in self.subagent_store.custom],
            "providers": providers,
            "tools": tools,
            "reasoning_efforts": [
                {"value": v, "label": REASONING_EFFORT_LABELS.get(v, v)}
                for v in REASONING_EFFORTS
            ],
        }

    async def save_subagent_builtin(
        self, agent_type: str, *, provider: str = "", model: str = "", reasoning: str = ""
    ) -> dict:
        """内置子代理的模型/思考强度覆盖（留空 = 跟随主对话）。"""
        if provider and provider not in self.cfg.providers:
            raise RuntimeError("未知的模型服务: " + provider)
        if agent_type not in BUILTIN_AGENT_TYPES:
            raise RuntimeError("未知的内置子代理: " + str(agent_type))
        try:
            ov = self.subagent_store.set_override(
                agent_type, provider=provider, model=model, reasoning=reasoning
            )
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        self._apply_registry_to_agents()
        return {"agent_type": agent_type, **ov.model_dump()}

    async def save_subagent_custom(
        self,
        *,
        name: str,
        description: str = "",
        prompt: str = "",
        tools="readonly",
        provider: str = "",
        model: str = "",
        enabled: bool = True,
    ) -> dict:
        """新建或更新一个自定义子代理（同名即更新），保存后立即可用。"""
        try:
            name = validate_subagent_name(name)
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        description = (description or "").strip()[:500]
        prompt = (prompt or "").strip()[:20_000]
        if provider and provider not in self.cfg.providers:
            raise RuntimeError("未知的模型服务: " + provider)
        known = {
            t.name for t in self._base_agent.registry.all()
        } - {"spawn_agent", "check_task"}
        if isinstance(tools, str):
            tools = tools.strip().lower() or "readonly"
            if tools not in ("all", "readonly"):
                raise RuntimeError("工具范围只能是 全部工具 / 仅只读 / 勾选工具列表")
        else:
            want = [str(n).strip() for n in (tools or []) if str(n).strip()]
            if not want:
                raise RuntimeError("自定义工具范围至少要勾选一个工具")
            unknown = sorted(set(want) - known)
            if unknown:
                raise RuntimeError("未知工具: " + "、".join(unknown))
            tools = want
        d = SubagentDef(
            name=name,
            description=description,
            prompt=prompt,
            tools=tools,
            provider=provider.strip(),
            model=model.strip(),
            enabled=bool(enabled),
        )
        self.subagent_store.upsert_custom(d)
        self._apply_registry_to_agents()  # 刷新 spawn_agent 的说明文字
        return d.model_dump()

    async def delete_subagent_custom(self, name: str) -> dict:
        try:
            self.subagent_store.remove_custom((name or "").strip())
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        self._apply_registry_to_agents()
        return {"removed": name}

    @staticmethod
    def _mask_key(key: str | None) -> str:
        if not key:
            return ""
        if len(key) <= 10:
            return "已设置"
        return key[:6] + "…" + key[-4:]

    async def providers_detail(self) -> dict:
        """设置页的模型服务清单（Key 只回传掩码，绝不回传全文）。"""
        detail = {}
        for name, pc in self.cfg.providers.items():
            key = resolve_api_key(name, pc)
            detail[name] = {
                "kind": pc.kind,
                "base_url": pc.base_url or "",
                "model": pc.model,
                "has_key": key is not None,
                "key_mask": self._mask_key(key),
                "key_from_env": bool(key and not pc.api_key),
                "is_default": name == self.cfg.default,
                "is_active": name == self.provider_name,
                "active_model": self.provider_model if name == self.provider_name else "",
                # 内置预设不可删除：删掉 config 里的条目它也仍会被合并回来
                "is_preset": name in PRESETS,
                "models": list(pc.models),
                "supports_reasoning": bool(pc.supports_reasoning),
                "supports_vision": bool(pc.supports_vision),
                "reasoning_effort": pc.reasoning_effort,
                "context_limit": pc.context_limit,
                "effective_context_limit": pc.effective_context_limit(
                    self.cfg.context_limit_tokens
                ),
                "global_context_limit": self.cfg.context_limit_tokens,
                "temperature": pc.temperature,
                "proxy": pc.proxy or "",
                "price_in": pc.price_in,
                "price_out": pc.price_out,
            }
        return {
            "config_path": str(config_path()),
            "home_dir": str(skysheep_home()),
            "db_path": str(db_path()),
            "default": self.cfg.default,
            "active": self.provider_name,
            "provider_error": self.provider_error,
            "disabled": disabled_providers_in_config(),  # 已隐藏的内置服务，可恢复
            "providers": detail,
        }

    async def add_provider(
        self,
        name: str,
        *,
        kind: str = "openai",
        base_url: str = "",
        model: str = "",
        api_key: str | None = None,
        set_default: bool = False,
        models: list[str] | None = None,
    ) -> dict:
        """新增自定义模型服务（写入 config.toml）；当前没有可用模型时顺带热启用。

        models 为添加时一并启用的模型列表（可多选），model 是其中当前使用的那个。
        """
        try:
            add_provider_to_config(
                name,
                kind=kind,
                base_url=base_url,
                model=model,
                api_key=api_key or None,
                set_default=set_default,
                models=models,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        clean = name.strip()
        activated = False
        # 缺模型时顺手把新服务用起来；已有可用模型就不打断用户当前选择
        if self.provider is None:
            try:
                self.provider = self._build_provider(clean)
                self.provider_error = None
                for ag in self._for_each_agent():
                    ag.provider = self.provider
                activated = True
            except RuntimeError as e:
                self.provider_error = str(e)
        return {
            "added": clean,
            "activated": activated,
            "provider_error": self.provider_error,
            "model": getattr(self.provider, "model", "") if activated else "",
        }

    async def delete_provider(self, name: str) -> dict:
        """从列表里删掉模型服务：自定义服务彻底删除，内置服务改为隐藏（可恢复）。"""
        try:
            remove_provider_from_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        was_active = name == self.provider_name
        if was_active:
            self.provider = None
            self.provider_name = ""
            # 让界面说明白"为什么现在没有模型可用"，而不是显示空的 provider/model
            self.provider_error = f"已删除正在使用的模型服务「{name}」，请重新选择一个"
        return {
            "removed": name,
            "default": self.cfg.default,
            "was_active": was_active,
            "hidden": name in PRESETS,  # 内置服务只是隐藏，可恢复
        }

    async def restore_provider(self, name: str) -> dict:
        """把隐藏的服务放回模型服务列表（内置服务按最新出厂预设重建）。"""
        try:
            restore_provider_in_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        return {"restored": name}

    async def set_provider_enabled(self, name: str, enabled: bool) -> dict:
        """启用/停用一个模型服务（停用只隐藏，配置数据保留）。"""
        try:
            if enabled:
                restore_provider_in_config(name)
            else:
                disable_provider_in_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        was_active = not enabled and name == self.provider_name
        if was_active:
            self.provider = None
            self.provider_name = ""
            self.provider_error = f"已停用正在使用的模型服务「{name}」，请重新选择一个"
        return {"name": name, "enabled": enabled, "was_active": was_active}

    async def probe_models(
        self,
        *,
        name: str = "",
        kind: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> dict:
        """检测某个模型服务当前可用的模型名（用于界面里一键选取，免手输）。

        界面上可能改了地址/Key 还没保存，所以优先用传入的值；没传的部分
        回落到该服务已保存的配置（含环境变量里的 Key）。
        """
        pc = self.cfg.providers.get(name) if name else None
        eff_kind = (kind or (pc.kind if pc else "openai")).strip()
        eff_url = (base_url or (pc.base_url if pc else "") or "").strip()
        eff_key = (api_key or "").strip()
        if not eff_key and pc is not None:
            eff_key = resolve_api_key(name, pc) or ""

        models = await probe_provider_models(
            kind=eff_kind, base_url=eff_url or None, api_key=eff_key or None
        )
        return {
            "provider": name,
            "kind": eff_kind,
            "base_url": eff_url,
            "count": len(models),
            "models": models,
        }

    async def save_provider(
        self,
        name: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        kind: str | None = None,
        set_default: bool = False,
        supports_reasoning: bool | None = None,
        supports_vision: bool | None = None,
        context_limit: int | None = None,
        temperature: float | str | None = None,
        price_in: float | None = None,
        price_out: float | None = None,
        proxy: str | None = None,
    ) -> dict:
        """保存 provider 字段到 config.toml 并热生效（若是当前使用的模型）。"""
        if name not in self.cfg.providers:
            raise RuntimeError("unknown provider: " + name)
        # 设为默认时把「当前正在用的模型」也固化进 config，下次启动即用它
        if set_default and not model and name == self.provider_name and self.provider_model:
            model = self.provider_model
        update_provider_in_config(
            name,
            base_url=(base_url or None),
            model=(model or None),
            api_key=(api_key or None),
            kind=(kind or None),
            set_default=set_default,
            supports_reasoning=supports_reasoning,
            supports_vision=supports_vision,
            context_limit=context_limit,
            temperature=temperature,
            price_in=price_in,
            price_out=price_out,
            proxy=proxy,
        )
        self.cfg = load_config()
        rebuilt = False
        if name == self.provider_name or self.provider is None:
            try:
                self.provider = self._build_provider(name)
                self.provider_error = None
                for ag in self._for_each_agent():
                    ag.provider = self.provider
                rebuilt = True
            except RuntimeError as e:
                self.provider_error = str(e)
        # 上下文上限跟服务走：改了当前服务的上限要立刻作用到活着的 Agent
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.context_limit_tokens = limit
        return {
            "saved": name,
            "rebuilt": rebuilt,
            "provider_error": self.provider_error,
            "model": getattr(self.provider, "model", ""),
            "reasoning": self.reasoning_state(),
        }

    async def _switch_after_removal(self) -> dict | None:
        """当前会话被删除/移走后：优先切到最近的其他会话；一个都不剩就回到"待新建"状态。"""
        latest = await self.store.latest_session(self.project.id)
        if latest is not None:
            await self.resume_session(latest.id)
            return {"id": latest.id, "title": latest.title}
        self.session = None
        self._base_agent.load_history([Message.system(self.compose_system())])
        return None

    async def delete_session(self, session_id: str) -> dict:
        # 先停掉该会话正在跑的 turn，再清理 runtime，最后删数据
        self.cancel_run(session_id)
        rt = self.runtimes.pop(session_id, None)
        if rt is not None:
            self._forget_runtime(rt)
        await self.store.delete_session(session_id)
        was_active = self.session is not None and self.session.id == session_id
        switched = await self._switch_after_removal() if was_active else None
        return {
            "deleted": session_id,
            "switched_to": switched,
            "new_active": self.session.id if self.session else None,
        }

    async def rename_session(self, session_id: str, title: str) -> dict:
        title = title.strip()
        if not title:
            raise RuntimeError("标题不能为空")
        await self.store.set_title(session_id, title)
        if self.session and self.session.id == session_id:
            self.session.title = title
        return {"id": session_id, "title": title}

    async def pin_session(self, session_id: str, pinned: bool) -> dict:
        await self.store.set_pinned(session_id, pinned)
        return {"id": session_id, "pinned": pinned}

    async def move_session(self, session_id: str, project_id: int | None) -> dict:
        """把会话移动到另一个项目；project_id=None 移入快聊。"""
        if project_id is not None:
            projects = {p.id: p for p in await self.store.list_projects()}
            if project_id not in projects:
                raise RuntimeError(f"project not found: {project_id}")
        await self.store.move_session(session_id, project_id)
        was_active = self.session and self.session.id == session_id
        moved_to_active = was_active and project_id == self.project.id
        switched = await self._switch_after_removal() if (was_active and not moved_to_active) else None
        return {"id": session_id, "project_id": project_id, "switched_to": switched}

    # ---- 工作项目切换（应用内设定工作目录，对标 Claude Code /add-dir 等） ----

    async def switch_project(self, path: str) -> dict:
        """把引擎整体切到另一个工作目录：项目记录（含白名单）、项目技能、
        项目级 MCP、AGENTS.md 项目记忆、子代理工作目录全部跟着换，
        然后接上该项目最近的会话。模型服务是全局配置，不受影响。
        """
        if self._run_task and not self._run_task.done():
            raise RuntimeError("当前有任务正在运行，请先停止再切换项目")
        target = Path(str(path).strip().strip('"')).expanduser()
        if not target.is_absolute():
            raise RuntimeError("请使用文件夹的完整路径，例如 D:\\works\\demo")
        target = target.resolve()
        if not target.is_dir():
            raise RuntimeError("目录不存在：" + str(target))
        if self.project is not None and str(target) == str(self.working_dir):
            return {
                "switched": False,
                "path": str(self.working_dir),
                "project": self.project.name,
                "reason": "这已经是当前项目",
            }

        # 旧项目的子代理还引用着旧工作目录，先全部停掉
        if self.tasks:
            self.tasks.cancel_all()

        self.working_dir = target
        self.project = await self.store.get_or_create_project(str(target))
        # 白名单按项目隔离：换项目 = 换一套规则；「自动允许写入」档跨项目保持
        prev_accept = self.gate.auto_accept_write if self.gate else False
        self.gate = PermissionGate(store=self.store, project_id=self.project.id,
                                   working_dir=self.working_dir)
        self.gate.auto_accept_write = prev_accept
        await self.gate.load_project_rules()
        self._base_agent.gate = self.gate

        self.skills = SkillLoader(
            global_dir=skysheep_home() / "skills",
            project_dir=target / ".skysheep" / "skills",
            state_path=target / ".skysheep" / "skills.json",
        )
        self.skills.discover()

        self.instructions_file, self.instructions_text = load_project_instructions(target)

        self.tasks = TaskManager(
            provider_factory=lambda: self.provider,
            working_dir=target,
            max_iterations=self.cfg.subagent_max_iterations,
            store=self.subagent_store,
            provider_resolver=self._subagent_provider,
            registry_resolver=self._subagent_registry,
        )

        # 项目级 mcp.json 指向新目录 → 重连；顺带用新技能/子代理重建完整注册表
        await self._reconnect_mcp()

        # 钩子与检查点跟项目走：钩子换工作目录，检查点换目录树，避免把
        # A 项目的文件快照回滚到 B 项目
        raw_cfg = load_raw_config()
        pre_rules, post_rules = hooks_from_config(raw_cfg)
        self.hooks = HookRunner(pre_rules, post_rules, working_dir=target) \
            if (pre_rules or post_rules) else None
        self.checkpoints = CheckpointStore(root=self._checkpoint_root())

        # 旧项目的会话 runtime 全部失效：停任务、释放、清空
        for rt in list(self.runtimes.values()):
            t = rt.run_task
            if t and not t.done():
                t.cancel()
            self._forget_runtime(rt)
        self.runtimes.clear()

        self._base_agent.working_dir = target
        self._base_agent.hooks = self.hooks
        for ag in self._for_each_agent():
            ag.working_dir = target
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())

        latest = await self.store.latest_session(self.project.id)
        if latest is not None:
            session_info = await self.resume_session(latest.id)
        else:
            self.session = None
            self._base_agent.load_history([Message.system(self.compose_system())])
            session_info = None
        return {
            "switched": True,
            "path": str(target),
            "project": self.project.name,
            "session": session_info,
            "mcp_warnings": self.mcp_warnings,
        }

    async def delete_project(self, project_id: int) -> dict:
        """把一个项目从列表里移除：项目记录、它的会话与白名单一并删除；
        电脑上的文件夹不受影响。当前正在使用的项目不允许删。"""
        if self.project is not None and project_id == self.project.id:
            raise RuntimeError("不能删除正在使用的项目；先切换到其他项目再删除")
        removed = await self.store.delete_project(project_id)
        if not removed:
            raise RuntimeError("项目不存在，可能已被删除")
        return {"removed": project_id}

    async def export_session(self, session_id: str, *, fmt: str = "md") -> dict:
        """导出会话：fmt=md 为 Markdown 原文；fmt=html 为带样式的自包含单文件。"""
        import re

        sess = await self.store.get_session(session_id)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        msgs = await self.store.load_messages(session_id)
        safe_title = re.sub(r"[^\w\-]+", "_", sess.title or sess.id)[:40] or sess.id
        if fmt == "html":
            body = []
            for m in msgs:
                if m.role == "system":
                    continue
                if m.role == "user":
                    body.append(f'<div class="msg user">{_html_escape(m.text)}</div>')
                elif m.role == "assistant":
                    body.append(f'<div class="msg ai">{_html_escape(m.text)}</div>')
                elif m.role == "tool":
                    for b in m.content:
                        content = getattr(b, "content", "")
                        err = " err" if getattr(b, "is_error", False) else ""
                        body.append(
                            f'<div class="msg tool{err}">🔧 工具结果 · '
                            f'<pre>{_html_escape(content[:1500])}</pre></div>'
                        )
            html = EXPORT_HTML_TEMPLATE.format(
                title=_html_escape(sess.title or sess.id),
                date=date.today().isoformat(),
                version=__version__,
                body="\n".join(body),
            )
            return {"filename": f"skysheep-{safe_title}.html", "html": html}
        lines = [f"# SkySheep 会话 · {sess.title or sess.id}", ""]
        for m in msgs:
            if m.role == "system":
                continue
            lines.append(f"## {m.role}")
            lines.append("")
            lines.append(m.to_plain())
            lines.append("")
        return {
            "filename": f"skysheep-{safe_title}.md",
            "markdown": "\n".join(lines),
        }

    # ---- 项目记忆（AGENTS.md 等，前端侧栏「项目记忆」入口） ----

    async def get_instructions(self) -> dict:
        text = ""
        if self.instructions_file:
            p = Path(self.instructions_file)
            if p.is_file():
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
        return {"path": self.instructions_file, "text": text}

    async def save_instructions(self, text: str) -> dict:
        """保存项目记忆并立即刷新系统提示词（对当前会话也生效）。"""
        text = text[:MAX_INSTRUCTIONS_CHARS]
        if self.instructions_file:
            path = Path(self.instructions_file)
        else:
            path = Path(self.working_dir) / "AGENTS.md"
            self.instructions_file = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.instructions_text = text
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        return {"saved": True, "path": str(path), "chars": len(text)}

    # ---- 会话库备份：列出 / 恢复 ----

    def list_session_backups(self) -> dict:
        """可恢复的会话库备份（含"当前"一项，便于对照时间）。"""
        items = self.store.list_backups()
        return {
            "backups": items,
            "dir": str(self.store.backup_dir()),
            "keep": self.store.BACKUP_KEEP,
        }

    async def restore_session_backup(self, name: str) -> dict:
        """从备份恢复会话库：先关库、换文件、重开，再重建内存状态。

        恢复后所有会话 runtime 作废（历史与消息 id 都可能对不上），
        重新挂到最新会话上；前端收到结果后重载列表即可。
        """
        name = str(name or "").strip()
        if not name:
            raise RuntimeError("缺少备份文件名")
        running = [
            rt for rt in self.runtimes.values() if rt.run_task and not rt.run_task.done()
        ]
        if running:
            raise RuntimeError("还有会话正在运行，先停止（Esc）或等它结束再恢复")
        try:
            result = await self.store.restore_backup(name)
        except (OSError, ValueError, FileNotFoundError) as e:
            raise RuntimeError(f"恢复失败：{e}") from None

        # 内存状态全部重建：旧 runtime 的历史/消息 seq 都可能与新库不一致
        for rt in list(self.runtimes.values()):
            self._forget_runtime(rt)
        self.runtimes.clear()
        self._base_queue.clear()
        self.session = None
        # 项目列表可能也随库回退了（备份里的项目集合是当时的样子）
        projects = await self.store.list_projects()
        cur = next(
            (p for p in projects if str(p.root_path).lower() == str(self.working_dir).lower()),
            None,
        )
        if cur is None:
            cur = await self.store.get_or_create_project(str(self.working_dir), self.working_dir.name)
        self.project = cur
        info = await self.open_initial_session()
        self._base_agent.load_history([Message.system(self.compose_system())])
        return {
            "restored": result["restored"],
            "safety_copy": result["safety_copy"],
            "session": info,
            "projects": [{"id": p.id, "name": p.name, "path": p.root_path} for p in projects],
        }

    # ---- 界面偏好（手调布局：侧栏宽 / 输入区高，前端拖拽后落盘 ui.json） ----
    # pywebview 默认 private_mode，前端 localStorage 每次启动都会清空，所以存后端文件。

    UI_PREFS_LIMITS = {
        "sidebar_w": (200, 460),
        "composer_h": (74, 520),
        "right_w": (240, 720),
        "right_collapsed": (0, 1),  # 右侧面板是否收起（1=收起，标签列表保留）
        "ui_scale": (70, 120),  # 界面缩放百分比（存 70–120 的整数，100 = 默认大小）
        "notify": (0, 1),  # Windows 系统通知开关（1=开，默认开）
        "pet": (0, 1),  # 对话区宠物「云朵小羊」开关（1=显示，默认显示）
        "accept_edits": (0, 1),  # 分级权限模式：1 = 自动允许写入（高危仍确认）
        # 宠物拖放位置（#chat 内布局像素）；上限给足，前端拖拽时已按容器收敛
        "pet_x": (0, 4000),
        "pet_y": (0, 4000),
        # 首次启动配置向导已完成标记（1=完成，不再自动弹出）
        "onboarded": (0, 1),
    }
    # 右侧面板：打开了哪些标签、激活的是哪个（id 白名单见前端 TAB_META）
    RIGHT_TAB_IDS = (
        "aux", "review", "terminal", "browser", "files",
        "tasks", "todo", "agenda", "cron", "memory",
    )
    # 字符串型偏好（值域白名单）：theme = auto | light | dark
    STRING_PREFS = {"theme": ("auto", "light", "dark")}

    def _ui_prefs_path(self) -> Path:
        return skysheep_home() / "ui.json"

    def _read_ui_prefs(self) -> dict:
        p = self._ui_prefs_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        prefs = {
            k: int(data[k])
            for k in self.UI_PREFS_LIMITS
            if isinstance(data.get(k), int) and not isinstance(data.get(k), bool)
        }
        tabs = data.get("right_tabs")
        if isinstance(tabs, list):
            cleaned: list[str] = []
            for t in tabs:
                if t in self.RIGHT_TAB_IDS and t not in cleaned:
                    cleaned.append(t)
            if cleaned:
                prefs["right_tabs"] = cleaned
        active = data.get("right_active")
        if isinstance(active, str) and active in self.RIGHT_TAB_IDS:
            prefs["right_active"] = active
        for key, allowed in self.STRING_PREFS.items():
            val = data.get(key)
            if isinstance(val, str) and val in allowed:
                prefs[key] = val
        return prefs

    # ---- Windows 系统通知（winotify toast；失败静默，非 Windows 平台不可用） ----

    def notify_enabled(self) -> bool:
        try:
            return self._read_ui_prefs().get("notify", 1) == 1
        except Exception:  # noqa: BLE001
            return True

    async def notify(self, params: dict) -> dict:
        """发一条 Windows toast（在独立线程里跑，不阻塞事件循环；失败静默）。"""
        title = str(params.get("title", ""))[:60] or "SkySheep"
        body = str(params.get("body", ""))[:160]
        if not self.notify_enabled():
            return {"sent": False, "reason": "disabled"}
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._toast_blocking, title, body)
            return {"sent": True}
        except Exception:  # noqa: BLE001 - 通知失败不影响主流程
            return {"sent": False}

    @staticmethod
    def _toast_blocking(title: str, body: str) -> None:
        from winotify import Notification, audio

        toast = Notification(app_id="SkySheep", title=title, msg=body)
        toast.set_audio(audio.Silent, loop=False)
        toast.show()

    async def get_ui_prefs(self) -> dict:
        return {"prefs": self._read_ui_prefs()}

    def first_paint_prefs(self) -> dict:
        """首帧外观偏好：由 server/app.py 注入到 index.html 的 <html> 标签。

        项目切换是整页 reload，若等 ui.get 回来才应用偏好，首帧必然是默认外观、
        随后再跳一次；这里提前给服务端用。读失败静默返回空（保持默认外观）。
        """
        try:
            return self._read_ui_prefs()
        except Exception:  # noqa: BLE001
            return {}

    async def save_ui_prefs(self, prefs: dict) -> dict:
        """合并保存；某项传 null 表示恢复默认（删除该项）。未知键忽略、越界值收敛到合法区间。"""
        current = self._read_ui_prefs()
        for key, val in (prefs or {}).items():
            if key not in self.UI_PREFS_LIMITS and key not in ("right_tabs", "right_active") \
                    and key not in self.STRING_PREFS:
                continue
            if val is None:
                current.pop(key, None)
                continue
            if key in self.STRING_PREFS:
                if isinstance(val, str) and val in self.STRING_PREFS[key]:
                    current[key] = val
                else:
                    current.pop(key, None)
                continue
            if key == "right_tabs":
                if isinstance(val, list):
                    cleaned: list[str] = []
                    for t in val:
                        if t in self.RIGHT_TAB_IDS and t not in cleaned:
                            cleaned.append(t)
                    if cleaned:
                        current["right_tabs"] = cleaned
                    else:
                        current.pop("right_tabs", None)
                continue
            if key == "right_active":
                if isinstance(val, str) and val in self.RIGHT_TAB_IDS:
                    current["right_active"] = val
                else:
                    current.pop("right_active", None)
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            lo, hi = self.UI_PREFS_LIMITS[key]
            current[key] = int(min(hi, max(lo, round(val))))
        path = self._ui_prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        return {"prefs": current}

    # ---- 全局记忆：设置页直接查看/编辑 memory.md ----

    async def memory_get(self) -> dict:
        from ..tools.memory import MAX_MEMORY_FILE_CHARS, memory_path

        p = memory_path()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        return {"path": str(p), "text": text[:MAX_MEMORY_FILE_CHARS]}

    async def memory_save(self, text: str) -> dict:
        from ..tools.memory import MAX_MEMORY_FILE_CHARS, memory_path

        p = memory_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text[:MAX_MEMORY_FILE_CHARS], encoding="utf-8")
        # 记忆注入系统提示词：保存后立刻对当前所有会话生效
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        return {"saved": True, "path": str(p), "chars": len(text)}

    # ---- 联网搜索 / AI 画图：设置页配置（写 config.toml + 热更新工具实例） ----

    async def websearch_detail(self) -> dict:
        ws = self.cfg.websearch
        resolved = resolve_websearch(self.cfg)
        key = resolved.get("api_key") if resolved else ""
        return {
            "provider": ws.provider,
            "providers": ["auto", "bocha", "tavily", "zhipu"],
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "config_hint": (
                "「自动」优先复用已配置 Key 的智谱服务；也可选博查（bocha.cn）或 "
                "Tavily 并填入对应 API Key，保存后立即生效。"
            ),
        }

    async def websearch_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "bocha", "tavily", "zhipu"):
            raise RuntimeError("联网搜索服务商只支持 auto / bocha / tavily / zhipu")
        updates: dict = {"provider": provider or None}
        api_key = params.get("api_key")
        if api_key is not None:
            updates["api_key"] = str(api_key).strip()
        update_config_section("websearch", updates)
        self.cfg = load_config()
        self._refresh_web_tool_configs()
        return await self.websearch_detail()

    async def imagegen_detail(self) -> dict:
        ig = self.cfg.imagegen
        resolved = resolve_imagegen(self.cfg)
        key = resolved.get("api_key") if resolved else ""
        return {
            "provider": ig.provider,
            "providers": ["auto", "zhipu", "siliconflow", "custom"],
            "model": ig.model,
            "base_url": ig.base_url,
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "resolved_model": (resolved or {}).get("model", ""),
            "config_hint": (
                "「自动」优先复用已配置 Key 的智谱 / 硅基流动服务；自定义需填 "
                "OpenAI 兼容的 /images/generations 接口地址与 Key。保存后立即生效。"
            ),
        }

    async def imagegen_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "zhipu", "siliconflow", "custom"):
            raise RuntimeError("画图服务商只支持 auto / zhipu / siliconflow / custom")
        updates: dict = {"provider": provider or None}
        for field_name in ("api_key", "model", "base_url"):
            if params.get(field_name) is not None:
                updates[field_name] = str(params[field_name]).strip()
        update_config_section("imagegen", updates)
        self.cfg = load_config()
        self._refresh_web_tool_configs()
        return await self.imagegen_detail()

    def _refresh_web_tool_configs(self) -> None:
        """把新的搜索/画图配置热更新到所有会话的工具实例上（不重建注册表）。"""
        ws_kw = self._websearch_kwargs() or {}
        ig_kw = self._imagegen_kwargs() or {}
        for ag in self._for_each_agent():
            if ag.registry is None:
                continue
            ws_tool = ag.registry.get("web_search")
            if ws_tool is not None:
                ws_tool.provider = ws_kw.get("provider", "")
                ws_tool.api_key = ws_kw.get("api_key", "")
            ig_tool = ag.registry.get("generate_image")
            if ig_tool is not None:
                ig_tool.provider = ig_kw.get("provider", "")
                ig_tool.api_key = ig_kw.get("api_key", "")
                ig_tool.base_url = ig_kw.get("base_url", "")
                ig_tool.model = ig_kw.get("model", "")

    # ---- 局域网访问：绑定开关 + 令牌（重启服务后生效；前端拼 URL 与二维码） ----

    async def lan_status(self) -> dict:
        server = self.cfg.server
        return {
            "enabled": bool(server.lan),
            "token": server.token,
            "ips": self._lan_ips(),
            "note": "" if server.lan else "局域网访问当前关闭：服务只监听本机 127.0.0.1。",
        }

    async def lan_enable(self, params: dict) -> dict:
        server = self.cfg.server
        token = str(params.get("token") or "").strip() or server.token or secrets.token_urlsafe(16)
        update_config_section("server", {"lan": True, "token": token})
        self.cfg = load_config()
        return {
            **await self.lan_status(),
            "note": "已开启局域网访问：重启 SkySheep 后生效（服务会监听全部网卡，"
                    "同一 Wi-Fi 下的设备凭令牌访问）。",
        }

    async def lan_disable(self) -> dict:
        update_config_section("server", {"lan": False})
        self.cfg = load_config()
        return {
            **await self.lan_status(),
            "note": "已关闭局域网访问：重启 SkySheep 后恢复仅本机监听。",
        }

    @staticmethod
    def _lan_ips() -> list[str]:
        """本机在局域网里的 IPv4（UDP connect 技巧，不真正发包）。"""
        ips: list[str] = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                obj = ipaddress.ip_address(ip)
                if not obj.is_loopback and not obj.is_link_local and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
        if not ips:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("10.255.255.255", 1))
                    ip = s.getsockname()[0]
                if ip and not ipaddress.ip_address(ip).is_loopback and ip not in ips:
                    ips.append(ip)
            except OSError:
                pass
        return ips

    # ---- 更新检查（设置 · 关于可手动触发；启动时后台已查过一次） ----

    async def check_update(self) -> dict:
        from ..core.uptodate import DEFAULT_RELEASES_API

        try:
            info = await check_latest_release(DEFAULT_RELEASES_API)
        except Exception as e:  # noqa: BLE001 - 手动检查要把失败原因说清楚
            self.update_error = str(e)
            self.update_info = None
            return {"available": False, "current": __version__, "error": str(e)}
        self.update_error = None
        if is_newer_version(info["version"], __version__):
            self.update_info = info
            return {"available": True, "current": __version__, **info}
        self.update_info = None
        return {"available": False, "current": __version__, "latest": info["version"]}

    # ---- 技能广场：远程索引优先，内置清单兜底（60s 缓存） ----

    async def market_list(self) -> dict:
        now = time.monotonic()
        if self._market_cache and now - self._market_cache[0] < 60:
            return self._market_cache[1]
        result = await fetch_market_index()
        self._market_cache = (now, result)
        return result

    # ---- 主题：标题栏联动（前端把解析后的主题回传给桌面壳） ----

    async def apply_theme(self, params: dict) -> dict:
        from .. import wintheme

        mode = "dark" if str(params.get("resolved", "light")) == "dark" else "light"
        wintheme.set_theme_mode(mode)
        # 事件驱动重刷标题栏：前端 CSS 变量是瞬时变色，而桌面壳看板线程每秒才
        # 轮询一次——不等这里推一把，页面和系统标题栏的变色时间肉眼可见地不一致。
        try:
            wintheme.refresh_now()
        except Exception:  # noqa: BLE001 - 浏览器模式无窗口 / 老系统无 DWM：静默
            pass
        return {"mode": mode}

    # ---- 快照 ----

    # 设置导出/导入覆盖的文件（~/.skysheep 下；会话数据库与检查点不导，体积大且含隐私对话）
    SETTINGS_EXPORT_FILES = (
        "config.toml", "mcp.json", "ui.json", "memory.md", "subagents.json",
    )

    async def settings_export(self) -> dict:
        """把配置类文件打包为 zip（base64 回传前端下载）。用于换机迁移/备份。"""
        import base64
        import io
        import zipfile

        buf = io.BytesIO()
        included = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in self.SETTINGS_EXPORT_FILES:
                p = skysheep_home() / name
                if p.is_file():
                    try:
                        zf.writestr(name, p.read_bytes())
                        included.append(name)
                    except OSError:
                        pass
            # 项目级 AGENTS.md 不在 home 下，跳过；技能目录打包清单文件（体积考虑，不含技能正文）
            skills = skysheep_home() / "skills"
            if skills.is_dir():
                manifest = [
                    {"name": s.name, "enabled": s.enabled}
                    for s in self.skills.all() if s.source == "global"
                ]
                zf.writestr("skills-manifest.json", json.dumps(manifest, ensure_ascii=False))
                included.append("skills-manifest.json")
        return {
            "filename": f"skysheep-settings-{date.today().isoformat()}.zip",
            "b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "included": included,
        }

    async def settings_import(self, params: dict) -> dict:
        """从导出的 zip 恢复设置（base64 上传）。覆盖同名文件，缺失的跳过。

        返回恢复清单；config.toml 恢复后由前端 boot() 重载生效。
        """
        import base64
        import io
        import zipfile

        raw = str(params.get("b64", "") or "")
        if not raw:
            raise RuntimeError("缺少 zip 内容（b64）")
        try:
            data = base64.b64decode(raw, validate=True)
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:
            raise RuntimeError(f"zip 解析失败: {e}") from None
        restored, skipped = [], []
        for name in zf.namelist():
            if name not in self.SETTINGS_EXPORT_FILES:
                continue  # skills-manifest.json 等：暂不自动恢复，避免覆盖现有技能状态
            try:
                target = skysheep_home() / name
                target.write_bytes(zf.read(name))
                restored.append(name)
            except OSError:
                skipped.append(name)
        if "config.toml" in restored:
            try:
                self.cfg = load_config()
            except Exception as e:
                raise RuntimeError(f"配置已写入但加载失败，请检查 config.toml：{e}") from None
        return {"restored": restored, "skipped": skipped}

    async def snapshot(self) -> dict:
        sessions = await self.store.list_sessions(self.project.id)
        return {
            "version": __version__,
            "working_dir": str(self.working_dir),
            "project": self.project.name,
            "provider": self.provider_name,
            "model": getattr(self.provider, "model", ""),
            "provider_error": self.provider_error,
            "supports_vision": self._supports_vision(),
            "context_limit": self._context_limit(),
            "providers": {
                name: {
                    "kind": pc.kind,
                    "model": pc.model,
                    "has_key": resolve_api_key(name, pc) is not None,
                    "supports_vision": bool(pc.supports_vision),
                }
                for name, pc in self.cfg.providers.items()
            },
            "session": (
                {
                    "id": self.session.id, "title": self.session.title,
                    "summary": getattr(self.session, "summary", ""),
                    "messages": (
                        [_msg_brief(m) for m in await self.store.load_messages(self.session.id)
                         if m.role in ("user", "assistant")]
                        if self.session else []
                    ),
                } if self.session else None
            ),
            "instructions_file": self.instructions_file,
            "sessions": [
                {"id": s.id, "title": s.title, "updated_at": s.updated_at}
                for s in sessions[:30]
            ],
            "skills": [
                {"name": s.name, "description": s.description, "source": s.source, "enabled": s.enabled}
                for s in self.skills.all()
            ],
            "skill_dirs": {
                "global": str(skysheep_home() / "skills"),
                "project": str(self.working_dir / ".skysheep" / "skills"),
            },
            "mcp": [
                {"name": n, "connected": st.connected, "error": st.error, "tools": st.tool_names}
                for n, st in self.mcp.statuses.items()
            ],
            "mcp_config": {
                "global": str(self._mcp_global_path()),
                "project": str(self._mcp_project_path()),
                "global_exists": self._mcp_global_path().exists(),
                "project_exists": self._mcp_project_path().exists(),
            },
            "mcp_presets": presets_public(),
            "mcp_installed": self.mcp_installed_names(),
            "mcp_warnings": self.mcp_warnings,
            # 首启向导：需要 Key 的内置服务预设（供前端渲染选择卡片），
            # signup 是该服务注册/建 Key 的控制台入口（无 Key 用户的第一跳）
            "provider_presets": [
                {"name": name, "base_url": pc.base_url or "", "model": pc.model,
                 "env_key": pc.env_key or "", "kind": pc.kind,
                 "signup": PRESET_SIGNUP_URLS.get(name, "")}
                for name, pc in PRESETS.items() if name != "ollama"
            ],
            # 崩溃哨兵：上次运行没有正常退出（崩溃/强杀）时为 True，
            # 前端据此提示用户导出诊断包（见 server/app.py 的 lifespan）
            "crashed_last_run": bool(getattr(self, "crashed_last_run", False)),
            "subagent": self.subagent_settings(),
            "update": self.update_info,
            "tools": [
                {"name": t.name, "safety": t.safety.value, "description": t.description}
                for t in self._base_agent.registry.all()
            ],
        }
