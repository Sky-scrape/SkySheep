"""SkySheep CLI：终端 REPL + 桌面启动器。

用法：
    skysheep chat [目录] [-p provider] [-s session_id]
    skysheep app [目录] [--port N] [--browser]
    skysheep run <任务> [--allow-tool ...] [--output text|json|stream]
    skysheep cron run <task_id>   # 立即执行一个定时任务后退出（系统计划任务入口）
    skysheep config init|path
    skysheep sessions
    skysheep models

会话内命令：
    /help /new /sessions /resume <id> /model [name] /tools /whitelist
    /skills /skill on|off <name> /mcp /cwd /export /exit
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
from pathlib import Path

from .. import __version__
from ..bgtasks import drain_bg_tasks, spawn_bg
from ..bootpages import error_html, splash_html
from ..config import (
    ConfigError,
    SkySheepConfig,
    config_path,
    db_path,
    load_config,
    resolve_imagegen,
    resolve_websearch,
    skysheep_home,
    write_config_template,
)
from ..core import Agent, build_system_prompt
from ..core.hooks import HookRunner, hooks_from_config, load_raw_config
from ..core.review import review_output
from ..core.subagent import CheckTaskTool, SpawnAgentTool, TaskManager
from ..core.team_store import TeamTemplateStore
from ..events import ErrorEvent, PermissionResolved, TextDelta, TurnFinished
from ..mcp import MCPManager, load_mcp_configs
from ..messages import Message
from ..models import Provider
from ..models.factory import build_provider
from ..security.gate import Decision, HeadlessGate
from ..security.trust import WorkspaceTrust
from ..server.backend_parts._shared import SessionRuntime
from ..server.backend_parts.automation import run_cron_task_core, run_cron_team_turn_core
from ..session import SessionStore, export_messages_text
from ..session.writer_lease import WriterLease
from ..skills import SkillLoader
from ..tools import ChangeRecorder, ToolRegistry, default_tools
from ..tools.memory import render_memory_section
from ..tools.shell import set_child_network
from ..tools.skill import LoadSkillTool


def _data_hint(rel: str) -> str:
    """数据目录下的路径提示：在用户主目录里显示成 ``~/...``，否则给绝对路径。

    源码运行默认 dev 身份（``~/.skysheep-dev``），安装版是 ``~/.skysheep``；
    提示文案跟着实际数据目录走，别把用户指到另一个身份的文件上。
    """
    home = skysheep_home()
    try:
        shown = "~/" + home.relative_to(Path.home()).as_posix()
    except ValueError:
        shown = str(home)
    return f"{shown}/{rel}"


@functools.lru_cache(maxsize=1)
def _console():
    """控制台惰性单例：rich 全家（render.py 还连带 markdown_it）只在真正要
    打印时才进内存——桌面启动路径只有横幅/兜底提示这几处用到，REPL 才是
    大主顾。prompt_toolkit 同理在 ChatApp.setup 里现取。"""
    from rich.console import Console

    return Console()


HELP_TEXT = """\
命令：
  /help                 显示本帮助
  /new                  新建会话
  /sessions             列出本项目最近会话
  /resume <id>          恢复指定会话
  /model [名称]          查看/切换模型 provider
  /tools                列出可用工具及安全级别
  /whitelist            查看本项目白名单规则
  /skills               列出技能（来源/状态）
  /skill on|off <名称>   开关某个技能
  /mcp                  查看 MCP 服务器连接状态与工具
  /cwd                  显示当前工作目录
  /export [文件]         导出当前会话为 Markdown
  /exit                 退出
快捷键：Ctrl-C 打断当前任务 / Ctrl-D 退出
"""

DECISION_MAP = {
    "y": Decision.ALLOW_ONCE,
    "a": Decision.ALLOW_ALWAYS,
    "n": Decision.DENY,
    "": Decision.DENY,  # 直接回车视为拒绝（安全默认）
}


class ChatApp:
    def __init__(self, args) -> None:
        self.args = args
        self.working_dir = Path(args.directory or ".").resolve()
        self.console = _console()
        from .render import Renderer

        self.renderer = Renderer(self.console)
        self.store: SessionStore | None = None
        self.project = None
        self.session = None
        self.cfg: SkySheepConfig | None = None
        self.provider: Provider | None = None
        self.provider_name = ""
        self.agent: Agent | None = None
        self.gate = None
        self.prompt = None  # PromptSession：REPL 模式 setup() 时现建（惰性导入）
        self.mcp: MCPManager | None = None
        self.skills: SkillLoader | None = None
        self.tasks: TaskManager | None = None
        self.trusted = False

    # ---- 启动 ----

    def _ask_trust(self, trust: WorkspaceTrust) -> bool:
        """终端里问一次「是否信任这个项目的自带配置」。

        默认拒绝（直接回车或非 y 都不信任）：这是降低防护的决定，不该因为用户
        随手回车就默认放行。信任后按项目路径记忆，指纹变了会重新问。
        """
        state = trust.state()
        _console().print(
            "[yellow]这个项目自带了会被自动执行的配置：[/yellow]"
        )
        for item in state.get("items", []):
            label = "MCP 服务器" if item.get("kind") == "mcp" else "技能"
            ro = "（其工具自动放行）" if item.get("readonly") else ""
            _console().print(f"  · {label} [bold]{item.get('name')}[/bold]：{item.get('detail')}{ro}")
        if state.get("changed"):
            _console().print("[yellow]（之前信任过，但配置已变更，需要重新确认）[/yellow]")
        _console().print(
            "先不信任也可以照常使用 Agent，只是这些项目自带配置本次不生效。"
        )
        try:
            answer = input("是否信任并启用？（y/N） ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _console().print("[dim]已跳过（本次不启用项目自带配置）[/dim]")
            return False
        if answer in ("y", "yes"):
            trust.grant()
            _console().print("[green]已信任：项目自带配置已启用[/green]")
            return True
        _console().print("[dim]未信任：项目自带配置本次不生效（下次打开还会问）[/dim]")
        return False

    async def setup(self) -> None:
        if not self.working_dir.exists():
            raise SystemExit("directory not found: " + str(self.working_dir))
        self.cfg = load_config()
        # 子进程环境口径（run_command / 终端 / 钩子 / MCP stdio / Mods Node）
        set_child_network(self.cfg.network)
        self.store = await SessionStore(db_path()).connect()
        self.project = await self.store.get_or_create_project(str(self.working_dir))
        self.gate = self._make_gate()
        await self.gate.load_project_rules()
        self.provider, self.provider_name = self._build_provider(self.args.provider or self.cfg.default)

        # workspace trust：项目自带的 mcp.json / skills 在确认前不生效（详见 security/trust.py）
        trust = WorkspaceTrust(skysheep_home(), self.working_dir)
        self.trusted = trust.is_trusted()
        if not self.trusted:
            self.trusted = self._ask_trust(trust)

        # M2: MCP 服务器连接（失败降级为警告）
        mcp_configs, mcp_config_warnings = load_mcp_configs(
            skysheep_home() / "mcp.json",
            (self.working_dir / ".skysheep" / "mcp.json") if self.trusted else None,
        )
        self.mcp = MCPManager(mcp_configs, network=self.cfg.network)
        # connect_all 返回工具列表（单值）；解析告警与连接失败状态合并
        mcp_tools = await self.mcp.connect_all()
        mcp_warnings = [
            *mcp_config_warnings,
            *(f"{n}: {st.error}" for n, st in self.mcp.statuses.items() if st.error),
        ]

        # M2: Skills 发现（全局 + 项目）
        self.skills = SkillLoader(
            global_dir=skysheep_home() / "skills",
            project_dir=(self.working_dir / ".skysheep" / "skills") if self.trusted else None,
            state_path=self.working_dir / ".skysheep" / "skills.json",
            scope_path=skysheep_home() / "skills-scope.json",
            project_root=self.working_dir,
        )
        self.skills.discover()

        # M2: 子代理任务簿
        self.tasks = TaskManager(
            provider_factory=lambda: self.provider,
            working_dir=self.working_dir,
            max_iterations=self.cfg.subagent_max_iterations,
            network=self.cfg.network,
        )

        registry = ToolRegistry(default_tools(
            store=self.store,
            websearch=resolve_websearch(self.cfg),
            imagegen=resolve_imagegen(self.cfg),
            computer_control=self.cfg.computer_control,
            browser_control=self.cfg.browser_control,
            network=self.cfg.network,
        ))
        registry.register(LoadSkillTool(self.skills))
        registry.register(SpawnAgentTool(self.tasks))
        registry.register(CheckTaskTool(self.tasks))
        for t in mcp_tools:
            registry.register(t)

        raw_cfg = load_raw_config()
        pre_rules, post_rules, stop_rules = hooks_from_config(raw_cfg)
        hooks = HookRunner(pre_rules, post_rules, working_dir=self.working_dir,
                           stop_rules=stop_rules) \
            if (pre_rules or post_rules or stop_rules) else None

        self.agent = Agent(
            provider=self.provider,
            registry=registry,
            gate=self.gate,
            working_dir=self.working_dir,
            max_iterations=self.cfg.max_iterations,
            context_limit_tokens=self.cfg.context_limit_tokens,
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            compaction_auto=self.cfg.compaction_auto,
            hooks=hooks,
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
        )
        self.agent.set_system(self._compose_system())

        history_file = config_path().parent / "cli-history"
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        self.prompt = PromptSession(history=FileHistory(str(history_file)))

        # 恢复会话或新建
        if self.args.session:
            sess = await self.store.get_session(self.args.session)
            if sess is None:
                raise SystemExit("session not found: " + str(self.args.session))
            self.session = sess
            msgs = await self.store.load_messages(sess.id)
            self.agent.load_history(msgs)
            self._banner(resumed=sess.id)
        else:
            await self._new_session()
        for w in mcp_warnings:
            self.console.print(f"[yellow]MCP 警告: {w}[/]")

    def _compose_system(self) -> str:
        return (
            build_system_prompt(self.working_dir)
            + self.skills.render_prompt_section()
            + render_memory_section()
        )

    def _make_gate(self):
        from ..security.gate import PermissionGate

        return PermissionGate(
            store=self.store, project_id=self.project.id,
            working_dir=self.working_dir,
        )

    def _build_provider(self, name: str):
        if name not in self.cfg.providers:
            raise SystemExit(
                "unknown provider: {}\navailable: {}".format(name, ", ".join(self.cfg.providers))
            )
        try:
            return build_provider(name, self.cfg.providers[name], self.cfg.network), name
        except ConfigError as e:
            raise SystemExit(
                str(e) + "\nhint: run `skysheep config init` and fill in the key"
            ) from e

    def _banner(self, resumed: str | None = None) -> None:
        model = f"{self.provider_name}/{self.provider.model}"
        self.console.print(
            f"[bold cyan]SkySheep[/] v{__version__} · "
            f"项目: [{self.project.name}]:{self.working_dir} · 模型: {model}"
        )
        if resumed:
            self.console.print(f"[dim]已恢复会话 {resumed}[/]")
        self.console.print("[dim]输入任务开始，/help 查看命令，Ctrl-D 退出[/]\n")

    async def _new_session(self) -> None:
        self.session = await self.store.create_session(self.project.id)
        self.agent.load_history([Message.system(self._compose_system())])
        self._banner()

    # ---- 权限确认交互 ----

    async def _ask_permission(self, ev) -> str:
        try:
            raw = await self.prompt.prompt_async(
                "[y] 允许一次  [a] 总是允许（本项目）  [n] 拒绝 > "
            )
        except (EOFError, KeyboardInterrupt):
            return Decision.DENY
        return DECISION_MAP.get(raw.strip().lower(), Decision.DENY)

    # ---- 主循环 ----

    async def _persist_new_messages(self, n_before: int) -> None:
        """把 history[n_before:] 增量落库（正常结束与打断共用的轮末收尾）。

        打断会把工具循环截断在中间：先补齐「有 tool_use 无 tool_result」的
        断口再落库，否则 /resume 与重启从库重载出的历史不满足协议
        （对照 agent.run_turn 轮首的 repair_dangling_tool_uses）。
        """
        self.agent.repair_dangling_tool_uses()
        for m in self.agent.history[n_before:]:
            await self.store.append_message(self.session.id, m)
        await self.store.touch(self.session.id)

    async def run(self) -> None:
        while True:
            try:
                line = await self.prompt.prompt_async("❯ ")
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]bye~[/]")
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                if await self.handle_command(text):
                    break
                continue

            if self.session and not self.session.title:
                self.session.title = text[:40]
                await self.store.set_title(self.session.id, self.session.title)

            n_before = len(self.agent.history)
            try:
                async for ev in self.agent.run_turn(text):
                    self.renderer.handle(ev)
                    if ev.kind == "permission_request":
                        decision = await self._ask_permission(ev)
                        self.agent.respond_permission(ev.request_id, decision)
            except (KeyboardInterrupt, asyncio.CancelledError) as e:
                # Ctrl-C 有两条到达路径，都必须接住：Python 3.11+ 的 asyncio.run
                # 会在轮中把第一次 Ctrl-C 投递为 CancelledError（KeyboardInterrupt
                # 只在 Runner 外层抛，Windows 上尤其如此），只接 KeyboardInterrupt
                # 会让打断分支不可达——异常穿透出去整个 REPL 直接退出。
                if isinstance(e, asyncio.CancelledError):
                    # 主任务吞掉这次取消后还要继续跑 REPL：退掉取消计数，
                    # 与 backend 轮次收尾对取消的处理取向一致
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
                self.renderer.close()
                self.console.print("\n[yellow]⏹ 已打断[/]")
            # 持久化本轮新增消息：打断的一轮也要落——用户消息在 run_turn 开头
            # 就进了内存 history，不落库的话 /resume 与重启从库重载后这一轮即
            # 丢失（对照 GUI 取消路径的 shield 落库）
            await self._persist_new_messages(n_before)
        self.renderer.close()
        await self.store.close()

    # ---- 斜杠命令 ----

    def _cmd_mcp(self) -> None:
        if not self.mcp or not self.mcp.statuses:
            self.console.print(f"[dim](未配置 MCP 服务器；参考 {_data_hint('mcp.json')})[/]")
            return
        for name, st in self.mcp.statuses.items():
            if st.connected:
                self.console.print("  [green]●[/] {}  [dim]{} 个工具: {}[/]".format(
                    name, len(st.tool_names), ", ".join(st.tool_names)))
            else:
                self.console.print("  [red]○[/] {}  [dim]{}[/]".format(name, st.error or "未连接"))

    def _cmd_skills(self) -> None:
        skills = self.skills.all()
        if not skills:
            hint = (
                f"(无技能；把 SKILL.md 放到 {_data_hint('skills/<名称>/')} "
                "或项目的 .skysheep/skills/<名称>/)"
            )
            self.console.print("[dim]" + hint + "[/]")
            return
        for s in skills:
            if not self.skills.applies(s.name):
                mark = "[yellow]--[/] "  # 范围不含本项目
            elif s.enabled:
                mark = "[green]on[/] "
            else:
                mark = "[red]off[/]"
            self.console.print(
                f"  {mark} {s.name:<18} [{s.source:^7}] {s.description}"
            )

    async def _cmd_skill_toggle(self, arg: str) -> None:
        parts = arg.split()
        if len(parts) != 2 or parts[0] not in ("on", "off"):
            self.console.print("[yellow]usage: /skill on|off <名称>[/]")
            return
        action, name = parts
        if self.skills.set_enabled(name, action == "on"):
            self.skills.discover()
            self.agent.set_system(self._compose_system())
            self.console.print(f"[green]skill {name} -> {action}（已更新系统提示词）[/]")
        else:
            self.console.print(f"[red]skill not found: {name}[/]")

    async def handle_command(self, line: str) -> bool:  # 返回 True 表示退出
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/exit", "/quit", "/q"):
            self.console.print("[dim]bye~[/]")
            return True
        elif cmd == "/help":
            self.console.print(HELP_TEXT)
        elif cmd == "/new":
            await self._new_session()
        elif cmd == "/sessions":
            sessions = await self.store.list_sessions(self.project.id)
            if not sessions:
                self.console.print("[dim](no sessions)[/]")
            for s in sessions[:20]:
                mark = "*" if self.session and s.id == self.session.id else " "
                self.console.print(
                    " {} [cyan]{}[/]  {}  [dim]{}[/]".format(
                        mark, s.id, _fmt_time(s.updated_at), s.title or "(untitled)"
                    )
                )
        elif cmd == "/resume":
            if not arg:
                self.console.print("[yellow]usage: /resume <id>[/]")
            else:
                sess = await self.store.get_session(arg)
                if sess is None:
                    self.console.print("[red]session not found: " + arg + "[/]")
                else:
                    self.session = sess
                    msgs = await self.store.load_messages(sess.id)
                    self.agent.load_history(msgs)
                    self.console.print(f"[green]resumed {sess.id}[/]")
        elif cmd == "/model":
            await self._cmd_model(arg)
        elif cmd == "/tools":
            for t in self.agent.registry.all():
                self.console.print(
                    f"  [cyan]{t.name:<12}[/] [{t.safety.value:>9}]  {t.description}"
                )
        elif cmd == "/whitelist":
            rules = await self.store.list_rules(self.project.id)
            if not rules:
                self.console.print("[dim](no rules for this project)[/]")
            for r in rules:
                # prefix 规则对 run_command 不覆盖 shell 拼接，列表里顺带说明，
                # 否则用户会奇怪「明明有 git status 规则，为什么拼接命令还问」
                note = ""
                if r["kind"] == "prefix" and r["tool"] == "run_command":
                    note = "[dim]（带 ; | & > 等拼接的命令不会命中）[/]"
                self.console.print(
                    "  {} {} {}{}".format(r["tool"], r["kind"], r["pattern"], note)
                )
        elif cmd == "/skills":
            self._cmd_skills()
        elif cmd == "/skill":
            await self._cmd_skill_toggle(arg)
        elif cmd == "/mcp":
            self._cmd_mcp()
        elif cmd == "/cwd":
            self.console.print(str(self.working_dir))
        elif cmd == "/export":
            # 相对路径相对项目工作目录解析（/cwd 显示的那个）：用户在 REPL
            # 里看到的目录与 /cwd 一致，导出文件不应落在进程启动目录里
            path = Path(arg) if arg else Path("skysheep-session.md")
            if not path.is_absolute():
                path = self.working_dir / path
            path.write_text(export_messages_text(self.agent.history), encoding="utf-8")
            self.console.print(f"[green]exported to {path}[/]")
        else:
            self.console.print(f"[yellow]unknown command: {cmd}[/]  (/help)")
        return False

    async def _cmd_model(self, arg: str) -> None:
        if not arg:
            for name, pc in self.cfg.providers.items():
                mark = " →" if name == self.provider_name else "  "
                self.console.print(
                    f" {mark} [cyan]{name:<12}[/] {pc.kind:<9} {pc.model}"
                )
            return
        if arg not in self.cfg.providers:
            self.console.print("[red]unknown provider: " + arg + "[/]")
            return
        try:
            self.provider, self.provider_name = self._build_provider(arg)
        except SystemExit as e:
            self.console.print("[red]" + str(e) + "[/]")
            return
        self.agent.provider = self.provider
        self.console.print(f"[green]switched to {arg}/{self.provider.model}[/]")


def _fmt_time(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


# ---- 子命令入口 ----


async def _chat_cmd(args) -> None:
    app = ChatApp(args)
    try:
        await app.setup()
        await app.run()
    finally:
        # setup 中途失败（如缺 API Key）也要清理资源，否则进程无法退出
        if app.mcp is not None:
            await app.mcp.shutdown()
        if app.tasks is not None:
            app.tasks.cancel_all()
        if app.store is not None:
            await app.store.close()


async def _sessions_cmd() -> None:
    store = await SessionStore(db_path()).connect()
    try:
        projects = {p.id: p for p in await store.list_projects()}
        sessions = await store.list_sessions(limit=50)
        if not sessions:
            print("(no sessions)")
            return
        for s in sessions:
            pname = projects[s.project_id].name if s.project_id in projects else "-"
            print("{:>2}  {}  [{}] {}  {}".format(
                "", s.id, pname, _fmt_time(s.updated_at), s.title or "(untitled)"
            ))
    finally:
        await store.close()


async def _models_cmd() -> None:
    cfg = load_config()
    print(f"已配置 providers（{_data_hint('config.toml')}）:")
    for name, pc in cfg.providers.items():
        mark = "→" if name == cfg.default else " "
        print(f"  {mark} {name:<12} {pc.kind:<9} {pc.model}")



def _last_line(text: str) -> str:
    """traceback 文本压缩成最后一行（真正那句异常消息），失败页只显示它。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


class _StartupLogCatcher(logging.Handler):
    """抄下 uvicorn 在 lifespan 启动失败时打的那条错误，取其中「真原因」那一行。

    uvicorn 的失败路径不把异常抛回调用方：它在服务线程里 ``sys.exit``，
    外层只看到「线程没了」。可启动失败页该给人看的是真原因（例如「会话数据库
    由更新版本的 SkySheep 创建（库版本 11 > 当前程序支持 8）」），所以这里挂个
    handler 把它抄下来。注意两种形状都要认：普通 logging 配置下是带 exc_info
    的记录；``log_level="warning"`` 这套配置把整段 traceback 当纯文本打
    （exc_info 为 None，2026-11-04 实测），此时取最后一行的异常消息。
    uvicorn 收尾那句 "Application startup failed. Exiting." 不含信息量，忽略；
    先抓到的那条留下，不被后续记录覆盖。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.detail = ""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.detail:
                return
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                text = str(exc) or type(exc).__name__
            else:
                text = record.getMessage()
            text = (text or "").strip()
            if not text or "Application startup failed" in text:
                return
            self.detail = _last_line(text)
        except Exception:  # noqa: BLE001 - 记日志失败不能反过来炸启动
            pass


def _port_open(port: int) -> bool:
    """本机环回端口是否可连接；主机固定为环回，不构造 URL。"""
    import socket

    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_ready(port: int, server, timeout_s: float = 60.0) -> bool:
    """等服务**真正**就绪：uvicorn 跑完 lifespan 启动（``server.started``）。

    只探测端口能不能连是不够的：端口通只说明 socket 起来了，不代表
    ``backend.setup()``（建库、跑迁移）成功。旧实现只看端口，于是「启动失败」
    与「启动很慢」在界面上无法区分——用户盯着启动动画干等满 60 秒超时才看到
    失败页，中途关窗就永远不知道原因（2026-11-04 实测：「库版本 N > 当前程序
    支持 M」就是这样被吞掉的）。``server.started`` 在 lifespan 成功之后才置
    True，是唯一可靠的信号。

    服务线程提前结束（lifespan 抛异常 → uvicorn ``sys.exit``）立即判失败，
    不必干等满 60 秒。超时预算本身不变：慢路径（数据库恢复、大库备份轮转、
    会话重开）仍要给够，宁可多等也不能把「慢」判成「死」。
    """
    import time

    has_started_flag = hasattr(server, "started")
    thread = getattr(server, "uvicorn_thread", None)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if has_started_flag:
            if server.started:
                return True
        elif _port_open(port):  # 老版本 uvicorn 没有 started 标志，只能退回端口探测
            return True
        if thread is not None and not thread.is_alive():
            return False
        time.sleep(0.1)
    return False


def _startup_banner(text: str) -> None:
    """启动横幅打印（服务已启动 / 局域网 / Tailscale 三句）：纯装饰。

    控制台句柄失效时（双击 .pyw 的分离进程没分配控制台、或启动瞬间控制台窗口
    被关掉），rich 的 LegacyWindowsTerm 走 WriteConsoleW 会报 ``OSError 22``——
    此时服务其实已经起来了、窗口还没开，这句提示若致命就会把启动炸成
    「启动失败」弹窗、留下一个无窗后端占着端口（2026-10-05 实测踩坑）。
    打印失败一律静默跳过。
    """
    try:
        _console().print(text)
    except Exception:  # noqa: BLE001 - 装饰性打印，任何原因失败都不致命
        pass


def _start_backend(args):
    """create_app + uvicorn 后台线程 + 等端口就绪。返回 (server, url)。"""
    import socket
    import threading

    import uvicorn

    from ..server import create_app

    cfg = load_config()
    # 局域网访问 / 远程访问（Tailscale）开启时监听全部网卡，凭令牌访问；默认只听本机。
    # 仅远程访问模式下，非 Tailscale 网段的来源会在 HTTP 守卫处被拒绝。
    host = "0.0.0.0" if (cfg.server.lan or cfg.server.tailscale) else "127.0.0.1"
    # 随机端口时把预绑定 socket 直接交给 uvicorn：先 close 再让 uvicorn 重 bind
    # 有个小窗口会被别的进程抢走（抢走后白等 60s 才判「启动失败」），占住不放没这个问题
    port = getattr(args, "port", 0) or 0
    if not port:
        # 配置里的固定端口（[server] port）：已安装的 PWA 把安装时的 origin
        # （含端口）固化进 start_url，端口随机则桌面重启后手机图标打开即死链。
        # 启动参数 --port 优先于配置；都没配仍是随机空闲端口。
        port = int(getattr(cfg.server, "port", 0) or 0)
    sockets = None
    if not port:
        s = socket.socket()
        s.bind((host, 0))
        port = s.getsockname()[1]
        sockets = [s]

    fast_app = create_app(working_dir=args.directory or ".", provider_name=args.provider)
    server = uvicorn.Server(
        uvicorn.Config(fast_app, host=host, port=port, log_level="warning")
    )
    # 让 backend 能请求优雅退出（一键重启用）：置 should_exit 后 uvicorn 会走完
    # lifespan 收尾（crash.flag 清除等）再退出线程，不是硬杀。
    fast_app.state.backend.request_shutdown = lambda: setattr(server, "should_exit", True)
    thread = threading.Thread(
        target=server.run,
        daemon=True,
        name="skysheep-uvicorn",
        kwargs={"sockets": sockets} if sockets is not None else {},
    )
    # 接住 lifespan 失败的真实原因：uvicorn 在服务线程里 sys.exit，异常不回流。
    catcher = _StartupLogCatcher()
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addHandler(catcher)
    try:
        thread.start()
        # 退出路径 join 用：置了 should_exit 还得等 lifespan 真正走完再退进程
        server.uvicorn_thread = thread
        ready = _wait_ready(port, server)
    finally:
        uvicorn_logger.removeHandler(catcher)
    if not ready:
        detail = catcher.detail or "后端未在限定时间内就绪，详见 logs/desktop.log"
        raise SystemExit(f"服务启动失败：{detail}")
    # 桌面窗口/浏览器兜底一律加载本机回环地址：0.0.0.0 只是绑定地址不是可访问地址，
    # 且守卫对回环永远免令牌，本机界面不受局域网/远程访问开关影响
    url = f"http://127.0.0.1:{port}/"
    _startup_banner(f"[bold cyan]SkySheep[/] 服务已启动: {url}")
    if cfg.server.lan:
        _startup_banner(
            "[yellow]局域网访问已开启：其他设备请用 "
            f"http://<本机IP>:{port}/?token=你的令牌 访问（令牌见 设置 · 手机控制）[/]"
        )
    if cfg.server.tailscale:
        _startup_banner(
            "[yellow]远程访问已开启（Tailscale）：手机登录同一 Tailscale 账号后，"
            f"在任意网络用 http://<电脑的Tailscale地址>:{port}/?token=你的令牌 访问"
            "（地址与二维码见 设置 · 手机控制）[/]"
        )
    return server, url


def _stop_backend(server, timeout_s: float = 5.0) -> None:
    """请求 uvicorn 优雅退出并**等它走完**。

    只置 should_exit 就返回的话，daemon 服务线程会被解释器直接掐掉，lifespan
    收尾（渠道/MCP/终端/数据库关闭、crash.flag 清除）走不完——表现为每次正常
    退出后，下次启动都提示「上次未正常关闭」。这里 join 服务线程直到收尾完成
    （上限 timeout_s，收尾卡住也不能拖住退出）。
    """
    if server is None:
        return
    try:
        server.should_exit = True
    except Exception:  # noqa: BLE001 - 退出路径不再抛
        pass
    thread = getattr(server, "uvicorn_thread", None)
    if thread is not None:
        try:
            thread.join(timeout=timeout_s)
        except RuntimeError:
            pass


def run_desktop_backend(directory: str, provider: str | None = None, port: int = 0):
    """desktop.py 的动画窗口就绪后调用：起引擎与服务，返回 (server, url)。

    刻意不碰 webview——窗口由 desktop.py 负责，这里只提供后端。
    """
    from types import SimpleNamespace

    return _start_backend(SimpleNamespace(directory=directory, provider=provider, port=port))


def _show_error_page(window, exc: BaseException) -> None:
    """把启动失败就地画进窗口；窗口已被关掉等情况静默放弃。"""
    from ..config import skysheep_home

    detail = str(exc) or type(exc).__name__
    try:
        from .. import wintheme

        window.load_html(error_html(detail, str(skysheep_home() / "logs" / "desktop.log"),
                                     theme=wintheme.current_theme_mode()))
    except Exception:  # noqa: BLE001
        pass


def _show_on_first_paint(window) -> None:
    """等启动动画页首帧就绪后再显示窗口（窗口 hidden=True 创建）。

    pywebview 先 Show() 窗口、WebView2 再异步初始化，两者之间的空窗期露出的是
    一块未绘制底色（用户看到的启动"黑屏"）。loaded 事件在页面就绪后触发，此时
    show 第一眼就是动画；4 秒兜底避免初始化异常时窗口藏死。
    """
    import threading

    def _show(*_args, **_kwargs) -> None:
        try:
            window.show()
        except Exception:  # noqa: BLE001
            pass

    try:
        window.events.loaded += _show
    except Exception:  # noqa: BLE001
        _show()
        return

    def _fallback() -> None:
        if not window.events.loaded.wait(4):
            _show()

    threading.Thread(target=_fallback, daemon=True).start()


def _app_cmd(args) -> None:
    """桌面模式：本地服务 + pywebview 原生窗口（浏览器兜底）。"""
    from ..obs import setup_file_logging

    # 源码入口也要接桌面日志：desktop.py 只在打包入口接线，`skysheep app` 直启
    # 不接的话 logs/desktop.log 永不生成，「轮次诊断」（diagnostics.turn_breakdown
    # 读该文件的 ev=turn 行）就恒空。只落文件不碰控制台，幂等可重复调用。
    setup_file_logging()
    import time
    import webbrowser

    import webview

    from .. import instance, previewwindows, wintheme
    from ..server.app import STATIC_DIR

    use_browser = getattr(args, "browser", False)
    if not use_browser:
        try:
            import webview  # pywebview
        except Exception as e:  # noqa: BLE001
            _console().print(f"[yellow]pywebview 不可用（{e}），改用浏览器打开[/]")
            use_browser = True

    if use_browser:
        server, url = _start_backend(args)
        webbrowser.open(url)
        _console().print("[dim]浏览器模式：Ctrl-C 退出[/]")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        # 等 lifespan 收尾真正走完再退进程，否则 crash.flag 残留、资源不收尾
        _stop_backend(server)
        return

    # 窗口模式（终端直接 skysheep app）：主窗口第一页是启动动画，引擎就绪后原地切换
    icon_path = STATIC_DIR / "skysheep.ico"
    from ..server.picker import FilePicker
    from ..webviewargs import apply_browser_args, apply_pending_reset

    # 关掉 Chromium 的滑动历史导航（触屏边缘滑、触控板横滑都会走它）：本入口原先
    # 只吃到 pywebview 的 IsSwipeNavigationEnabled（只覆盖触屏），触控板那条路会画出
    # 圆形后退提示并真的后退一次。必须在 webview.start() 之前设置（见 webviewargs）
    # 排队的「清除全部浏览器数据」同样在这里落地：窗口还没建，profile 文件没人占用
    apply_browser_args()
    apply_pending_reset()

    try:
        screen = webview.screens[0]
    except Exception:  # noqa: BLE001
        screen = None

    width, height = 1360, 860
    x = y = None
    if screen is not None:
        # 小屏适配：窗口不许比屏幕大（否则四周溢出、看起来偏右下）；纵向略偏上给任务栏留空间
        width = max(980, min(width, screen.width - 24))
        height = max(640, min(height, screen.height - 80))
        x = screen.x + (screen.width - width) // 2
        y = screen.y + max((screen.height - height) // 3, 16)

    picker = FilePicker()
    # 主题尽量提前定下来：窗口底色 / 启动页 / 标题栏用同一份解析结果
    wintheme.set_theme_mode(wintheme.read_ui_theme())
    window = webview.create_window(
        instance.app_title(),
        html=splash_html(STATIC_DIR, theme=wintheme.current_theme_mode()),
        width=width,
        height=height,
        min_size=(980, 640),
        js_api=picker,
        x=x,
        y=y,
        # 允许选中页面文字（pywebview 默认给 body 注入 user-select:none）：
        # 「引用回答片段」等选区交互依赖它
        text_select=True,
        # 先隐藏创建，等启动动画页首帧就绪（loaded）再显示：窗口 Show 与 WebView2
        # 初始化是异步的，控件首帧之前整窗是一块未绘制底色（启动时的"黑屏"）。
        hidden=True,
        # 窗口底色跟主题：默认白底在整页刷新（切项目）/首帧瞬间会露出来
        background_color=wintheme.window_background(),
    )
    _show_on_first_paint(window)
    picker.attach(window)
    from ..wintheme import allow_microphone, hook_caption_theme, register_window

    hook_caption_theme(window)  # 标题栏染成纸墨主题色（老系统自动跳过）
    allow_microphone(window)  # 放行麦克风（语音输入用；WebView2 默认静默拒绝）
    register_window(window)  # 前端切主题时事件驱动重刷标题栏（与 desktop.py 同机制）

    # 独立预览窗：注册窗口工厂。后端在 WS 线程调用 webview.create_window——
    # pywebview 对非主线程会自己 Invoke 到 GUI 线程（见 winforms.create_window），
    # 同一进程、同一 WebView2 profile，不另起服务。主窗口真正关闭时一并收掉，
    # 否则最后一个窗关不掉会让 webview.start() 一直不返回。
    def _preview_window_factory(url: str, title: str):
        # 尺寸按屏幕回收（与主窗口同一口径）：1120×780 在 2 倍缩放的笔记本上
        # 会高过屏幕，底边跑到屏外
        try:
            scr = webview.screens[0]
            pw = max(640, min(1120, scr.width - 40))
            ph = max(480, min(780, scr.height - 80))
        except Exception:  # noqa: BLE001
            pw, ph = 1120, 780
        win = webview.create_window(
            title, url=url, width=pw, height=ph, min_size=(520, 360),
            text_select=True, background_color=wintheme.window_background(),
        )
        if win is not None:
            hook_caption_theme(win)
        return win

    previewwindows.register(_preview_window_factory)
    window.events.closed += lambda *_: previewwindows.close_all_async()

    def _bootstrap() -> None:
        """GUI 循环启动后执行：后台起引擎与服务，就绪后本窗口内切换到应用页。"""
        try:
            server, url = _start_backend(args)
        except BaseException as exc:  # noqa: BLE001  就地显示失败页；关闭窗口后外层再上报
            errors.append(exc)
            _show_error_page(window, exc)
            return
        # 先登记再切页：切页失败（窗口被提前关掉）时外层收尾仍能停到服务
        running["server"] = server
        try:
            window.load_url(url)
        except Exception:  # noqa: BLE001  窗口被用户提前关掉：无处可切，静默收场
            pass

    errors: list[BaseException] = []
    running: dict[str, object] = {}
    # 固定 profile 目录并关闭私有模式（与 desktop.py 同一优化）：默认私有模式
    # 每次启动都拿一个全新临时 profile，从不清理（每次启动往 TEMP 漏一个），
    # 浏览器环境每次从零初始化、首帧明显变慢。目录放在实例数据目录下的
    # webview-app/，与桌面壳的 webview/ 各用各的——本路径没有单实例互斥体，
    # 两个入口同开时不会争同一个 WebView2 profile（同路径并发第二个窗口会
    # 初始化失败）；同一个入口连开两份属于边缘用法，将争用同一 profile。
    webview.start(
        _bootstrap,
        private_mode=False,
        storage_path=str(instance.data_home() / "webview-app"),
        icon=str(icon_path) if icon_path.exists() else None,
    )
    previewwindows.close_all()
    previewwindows.unregister()
    if errors:
        raise errors[0]
    _stop_backend(running.get("server"))


# ---- headless 一次性运行（对标 claude -p / codex exec / gemini -p） ----


async def _assemble_headless_engine(store, working_dir: Path, cfg, provider_obj, trusted: bool):
    """CLI 无人值守入口（skysheep run / cron run）共用的引擎装配。

    技能发现 → MCP 连接 → 子代理任务簿 → 工具注册表 → 钩子，与后端
    _build_full_registry 同一能力面。返回 (skills, mcp, tasks, registry,
    hooks, team_templates)；mcp / tasks 由调用方在 finally 里收尾
    （shutdown / cancel_all）。team_templates 是建队模板册（定时团队二期，
    §14.6：cron run 的团队任务凭它按模板建队——成员解析等价物在
    run_cron_team_turn_core，CLI 只补装配），只有 cron run 的团队任务消费。
    """
    team_templates = TeamTemplateStore(skysheep_home() / "teams.json")
    team_templates.load()

    skills = SkillLoader(
        global_dir=skysheep_home() / "skills",
        project_dir=(working_dir / ".skysheep" / "skills") if trusted else None,
        state_path=working_dir / ".skysheep" / "skills.json",
        scope_path=skysheep_home() / "skills-scope.json",
        project_root=working_dir,
    )
    skills.discover()

    mcp_configs, _mcp_config_warnings = load_mcp_configs(
        skysheep_home() / "mcp.json",
        (working_dir / ".skysheep" / "mcp.json") if trusted else None,
    )
    mcp = MCPManager(mcp_configs, network=cfg.network)
    mcp_tools = await mcp.connect_all()

    tasks = TaskManager(
        provider_factory=lambda: provider_obj,
        working_dir=working_dir,
        max_iterations=cfg.subagent_max_iterations,
        network=cfg.network,
    )
    registry = ToolRegistry(default_tools(
        store=store,
        websearch=resolve_websearch(cfg),
        imagegen=resolve_imagegen(cfg),
        computer_control=cfg.computer_control,
        browser_control=cfg.browser_control,
        network=cfg.network,
    ))
    registry.register(LoadSkillTool(skills))
    registry.register(SpawnAgentTool(tasks))
    registry.register(CheckTaskTool(tasks))
    for t in mcp_tools:
        registry.register(t)

    raw_cfg = load_raw_config()
    pre_rules, post_rules, stop_rules = hooks_from_config(raw_cfg)
    hooks = HookRunner(pre_rules, post_rules, working_dir=working_dir,
                       stop_rules=stop_rules) \
        if (pre_rules or post_rules or stop_rules) else None
    return skills, mcp, tasks, registry, hooks, team_templates


def _ndjson_write(payload: dict) -> None:
    """NDJSON 协议一行落 stdout：按 UTF-8 字节写出并立即冲刷，
    绕开 Windows 控制台 GBK 编码（含 emoji 的回答会崩 print）。"""
    import json as _json
    import sys as _sys

    line = _json.dumps(payload, ensure_ascii=False)
    # U+2028（行分隔符）/ U+2029（段落分隔符）是 JSON 字符串的合法内容，
    # ensure_ascii=False 下 dumps 原样保留；但按 Unicode 行界切行的下游
    # （部分语言的 str.splitlines、某些 NDJSON 解析器）会把它们当换行，
    # 把一行事件切裂。转成字面转义序列 \u2028 / \u2029——JSON 语义完全
    # 等价（json.loads 解回同一码点），行结构恢复「一行一事」。
    line = line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    buf = getattr(_sys.stdout, "buffer", None)
    if buf is None:
        print(line, flush=True)
    else:
        buf.write((line + "\n").encode("utf-8"))
        buf.flush()


async def run_headless(
    prompt: str,
    directory: str = ".",
    provider: str | None = None,
    allow_tools: list[str] | None = None,
    output: str = "text",
    max_iterations: int = 0,
    provider_factory=None,
    trust_project: bool = False,
) -> dict:
    """非交互跑一轮任务：完整工具链 + 无人值守门控（只读放行，预授权名单放行，
    其余自动拒绝），消息落库可审计。返回结构化结果；失败抛 SystemExit(2)。
    output="stream" 时逐事件以 NDJSON 行落 stdout（协议见 docs/无头运行协议.md），
    末行为 kind="result" 的汇总行。"""
    working_dir = Path(directory).resolve()
    if not working_dir.exists():
        raise SystemExit("directory not found: " + str(working_dir))
    cfg = load_config()
    name = provider or cfg.default
    if provider_factory is None and name not in cfg.providers:
        raise SystemExit(
            "unknown provider: {}; available: {}".format(name, ", ".join(cfg.providers))
        )
    if provider_factory is not None:
        provider_obj = provider_factory()
        provider_name = "fake"
    else:
        try:
            provider_obj = build_provider(name, cfg.providers[name], cfg.network)
        except ConfigError as e:
            raise SystemExit(str(e) + "\nhint: skysheep config init 后填入 API Key") from e
        provider_name = name

    store = await SessionStore(db_path()).connect()
    mcp = None
    tasks = None
    try:
        project = await store.get_or_create_project(str(working_dir))
        gate = HeadlessGate(
            allowed=[t.strip() for t in (allow_tools or []) if t.strip()],
            store=store, project_id=project.id, working_dir=working_dir,
        )
        await gate.load_project_rules()

        # workspace trust：无人值守场景没有界面可问，只能用显式参数或已有信任记录
        trusted = WorkspaceTrust(skysheep_home(), working_dir).is_trusted()
        if trust_project and not trusted:
            WorkspaceTrust(skysheep_home(), working_dir).grant()
            trusted = True

        skills, mcp, tasks, registry, hooks, _team_templates = \
            await _assemble_headless_engine(store, working_dir, cfg, provider_obj, trusted)

        agent = Agent(
            provider=provider_obj,
            registry=registry,
            gate=gate,
            working_dir=working_dir,
            max_iterations=max_iterations or cfg.max_iterations,
            context_limit_tokens=cfg.context_limit_tokens,
            compaction_keep_recent=cfg.compaction_keep_recent,
            compaction_auto=cfg.compaction_auto,
            hooks=hooks,
            job_containment=cfg.shell.job_containment,
            sandbox_level=cfg.shell.sandbox_level,
            use_ripgrep=cfg.search.use_ripgrep,
        )
        agent.set_system(
            build_system_prompt(working_dir)
            + skills.render_prompt_section()
            + render_memory_section()
        )

        sess = await store.create_session(project.id, title="▶ " + prompt[:40])
        n_before = len(agent.history)
        error: str | None = None
        stop_reason = "error"
        interrupted = False  # 取消/异常中断：落库与 stream 末行照常走，语义如实
        try:
            async for ev in agent.run_turn(prompt):
                if output == "stream":
                    _ndjson_write(ev.model_dump(mode="json"))
                if isinstance(ev, TextDelta) and output == "text":
                    print(ev.text, end="", flush=True)
                elif isinstance(ev, ErrorEvent):
                    error = ev.message
                elif isinstance(ev, TurnFinished):
                    stop_reason = ev.stop_reason
            if output == "text" and stop_reason != "max_iterations":
                print()  # 流式文本后补换行
        except (KeyboardInterrupt, asyncio.CancelledError):
            # 用户停止（asyncio.run 下第一次 Ctrl-C 投递为 CancelledError，
            # KeyboardInterrupt 只在 Runner 外层抛）：吞掉这次取消继续收尾
            # （对照 REPL 的 uncancel 取向），末行 stop_reason 如实标 cancelled
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            interrupted = True
            stop_reason = "cancelled"
        except Exception as e:  # noqa: BLE001 - 落库收尾后原样上抛，语义不变
            # 意外异常：已产生的消息与部分回答不能随异常无声丢失
            interrupted = True
            stop_reason = "error"
            error = error or f"{type(e).__name__}: {e}"
            raise
        finally:
            # 打断也要落（对照 REPL 与 backend _persist_turn 的标准）：
            # 正常 / 取消 / 异常三条路径都执行——消息照常落库，stream 模式
            # 补 result 末行，消费方按「末行必有 result」解析才不会挂起。
            # 异常路径 raise 穿过本块：先收尾、后上抛。
            new_msgs = agent.history[n_before:]
            for m in new_msgs:
                await store.append_message(sess.id, m)
            await store.touch(sess.id)
            reply = next(
                (m.text for m in reversed(new_msgs)
                 if m.role == "assistant" and m.text.strip()),
                "",
            )
            result = {
                "session_id": sess.id,
                "provider": provider_name,
                "model": getattr(provider_obj, "model", ""),
                "stop_reason": stop_reason,
                "error": error,
                "reply": reply.strip(),
                "messages": [
                    {"role": m.role, "text": m.text}
                    for m in new_msgs if m.role in ("user", "assistant")
                ],
                "usage": {
                    "input": agent.total_in_tokens,
                    "output": agent.total_out_tokens,
                },
            }
            if output == "stream":
                # 末行汇总：messages 不重复给——完整消息已由 assistant_message 事件流承载
                _ndjson_write({"kind": "result", **{
                    k: v for k, v in result.items() if k != "messages"}})
            if interrupted and output == "text":
                print()  # 打断的流式文本后也补换行，别让提示符接在半句上
        return result
    finally:
        if mcp is not None:
            await mcp.shutdown()
        if tasks is not None:
            tasks.cancel_all()
        await store.close()


def _run_cmd(args) -> None:
    import sys as _sys

    try:
        result = asyncio.run(run_headless(
            prompt=args.prompt,
            directory=args.directory,
            provider=args.provider,
            allow_tools=[t for t in (args.allow_tool or "").split(",") if t.strip()],
            output=args.output,
            max_iterations=args.max_iterations,
            trust_project=bool(getattr(args, "trust_project", False)),
        ))
    except SystemExit as e:
        if e.code and not isinstance(e.code, int):
            print(e.code, file=_sys.stderr)
        raise SystemExit(2) from None
    if args.output == "json":
        import json as _json

        print(_json.dumps(result, ensure_ascii=False))
    elif args.output != "stream":
        # stream 模式的全部输出（事件行 + 末行汇总）已在 run_headless 内落 stdout
        if result["reply"]:
            print(result["reply"])
        if result.get("error"):
            _console().print("[red]✗ " + result["error"] + "[/]")
    # 退出码：正常收尾 0；出错/被打断/异常中断 1 —— 方便脚本与 CI 判断
    if result.get("error") or result["stop_reason"] != "end_turn":
        raise SystemExit(1)


def _build_sendonly_channels(cfg):
    """一次性进程的渠道管理器：只构建适配器实例（能 send_text），不 start()。

    `skysheep cron run` 是短进程，只发终态摘要、不收消息——不启动收消息的
    长连接（lark-cli WebSocket），也就不与桌面端同时在线的机器人抢事件。
    渠道装配复用 ChannelManager 同一份 build_channels，口径一致。
    """
    from ..channels.manager import ChannelManager, build_channels

    mgr = ChannelManager(None, lambda: {})  # host 仅收消息路由用；只发不收
    mgr.channels = build_channels(dict(cfg.channels.platforms or {}), lambda _msg: None)
    return mgr


# 退出前等在途终态推送落地的上限（秒）：webhook 单发默认 10s 超时、
# 飞书走 lark-cli 子进程，正常推送远用不满；坏端点最多拖 30s 必退出
CRON_PUSH_DRAIN_TIMEOUT_S = 30.0


async def _cron_run_async(
    task_id: int, provider_name: str | None, project_dir: str | None = None
) -> int:
    """`skysheep cron run <task_id>` / `skysheep cron-run <task_id>` 的主体：
    引导引擎、执行该任务、回写、退出。

    供 Windows 任务计划程序导出/自动注册的系统计划任务调用（应用不开着也能
    到点跑）。执行/结果回写/下次排期/notify_channel 推送全部走
    automation.run_cron_task_core（与后端扫描循环同一条路径）；这里的差异只有
    CLI 自己的引擎装配（对标 run_headless）与只发不收的渠道实例。
    工作目录按任务归属项目从库里解析（计划任务的进程 cwd 不可靠）；
    project_dir 显式给出时优先（系统级定时调度注册的 /TR 里写死的目录），
    但任务归属项目行仍必须存在且目录可解析，悬空任务照旧拒绝执行。
    SKYSHEEP_HOME 照常生效。返回进程退出码：完成 0 / 失败 1。
    """
    cfg = load_config()
    name = provider_name or cfg.default
    if name not in cfg.providers:
        raise SystemExit(
            "unknown provider: {}; available: {}".format(name, ", ".join(cfg.providers))
        )
    try:
        # 全局出网设置必须跟着走（代理 / no_proxy / 自签 CA）：无人值守的
        # 模型流量与 run_headless / REPL / backend 全部装配点同一口径，
        # 漏传会静默绕过企业代理与自签 CA 直连
        provider_obj = build_provider(name, cfg.providers[name], cfg.network)
    except ConfigError as e:
        raise SystemExit(str(e) + "\nhint: skysheep config init 后填入 API Key") from e

    store = await SessionStore(db_path()).connect()
    mcp = None
    tasks = None
    try:
        task = await store.get_cron_task(task_id)
        if task is None:
            raise SystemExit(f"定时任务不存在: {task_id}")
        proj = await store.get_project(int(task.get("project_id") or 0))
        if proj is None or not Path(proj.root_path).is_dir():
            raise SystemExit("任务所属的项目目录已不存在，无法执行")
        # 产线级模型：与桌面端同一口径（空 = 跟随全局）。CLI 的定时预览
        # 也要认这两列，否则同一条任务在 CLI 与桌面跑的是两个模型。
        line_provider = str(task.get("provider_name") or "").strip()
        context_limit = cfg.context_limit_tokens
        if line_provider:
            pc = cfg.providers.get(line_provider)
            if pc is None:
                raise SystemExit(f"产线指定的模型服务「{line_provider}」不存在或已停用")
            line_model = str(task.get("model") or "").strip()
            if line_model:
                pc = pc.model_copy(update={"model": line_model})
            try:
                provider_obj = build_provider(line_provider, pc, cfg.network)
            except ConfigError as e:
                raise SystemExit(str(e) + "\nhint: skysheep config init 后填入 API Key") from e
            context_limit = pc.effective_context_limit(cfg.context_limit_tokens)
        working_dir = Path(proj.root_path)
        if project_dir:
            # 系统计划任务的 /TR 里写死了注册时的项目目录：计划任务进程的
            # cwd 不可靠，显式目录优先于库里的记录（两者正常情况下一致）
            explicit = Path(project_dir).expanduser()
            if not explicit.is_dir():
                raise SystemExit("指定的项目目录不存在: " + str(project_dir))
            working_dir = explicit.resolve()

        # 信任口径与 run_headless 一致：有过信任记录才启用项目自带的 mcp/技能。
        # 团队任务与单会话任务共用这条装配链（二期 §14.6：团队成员的工具表、
        # 模板册都在这里装，一期「团队任务跳过装配直接拒绝」的口径移除）
        trusted = WorkspaceTrust(skysheep_home(), working_dir).is_trusted()
        skills, mcp, tasks, registry, hooks, team_templates = \
            await _assemble_headless_engine(store, working_dir, cfg, provider_obj, trusted)

        async def agent_turn(t: dict):
            # 与后端 _execute_cron_turn 同构：独立会话 + HeadlessGate + 一轮
            sess = await store.create_session(t["project_id"], title=f"⏰ {t['name']}")
            sid = sess.id
            gate = HeadlessGate(allowed=t["allowed_tools"], store=store,
                                project_id=t["project_id"], working_dir=working_dir)
            agent = Agent(
                provider=provider_obj,
                registry=registry,
                gate=gate,
                working_dir=working_dir,
                max_iterations=cfg.max_iterations,
                context_limit_tokens=context_limit,
                compaction_keep_recent=cfg.compaction_keep_recent,
                compaction_trigger=cfg.compaction_trigger,
                compaction_auto=cfg.compaction_auto,
                hooks=hooks,
                restrict_to_workdir=cfg.restrict_to_workdir,
                session_id=sid,
                job_containment=cfg.shell.job_containment,
                sandbox_level=cfg.shell.sandbox_level,
                use_ripgrep=cfg.search.use_ripgrep,
            )
            agent.set_system(
                build_system_prompt(working_dir)
                + skills.render_prompt_section()
                + render_memory_section()
            )
            runtime = SessionRuntime(sid=sid, agent=agent, recorder=ChangeRecorder())
            n_before = len(agent.history)
            error = None
            stop_reason = "error"
            denied = 0  # 被无人值守门控拒绝的工具次数（预授权没勾全的痕迹）
            async for ev in agent.run_turn(t["prompt"]):
                if isinstance(ev, ErrorEvent):
                    error = ev.message
                elif isinstance(ev, TurnFinished):
                    stop_reason = ev.stop_reason
                elif isinstance(ev, PermissionResolved) and ev.decision == "deny":
                    denied += 1
            # 消息落库（run_headless 同款）：异常结束的一轮也要可审计
            for m in agent.history[n_before:]:
                await store.append_message(sid, m)
            await store.touch(sid)
            try:
                await store.add_usage(
                    sid, getattr(provider_obj, "name", "") or name,
                    getattr(provider_obj, "model", ""),
                    agent.total_in_tokens, agent.total_out_tokens,
                    agent.total_cached_tokens,
                )
            except Exception:  # noqa: BLE001 - 用量记录失败不影响执行
                pass
            # 验收工位（可选，默认关）：只读、单轮，结论交给 run_cron_task_core
            # 与自动判定合并（只能降级，不能升级）
            review = None
            if (t.get("review") and error is None
                    and stop_reason in ("end_turn", "max_iterations")):
                last_text = ""
                for m in reversed(agent.history[n_before:]):
                    text = str(getattr(m, "text", "") or "").strip()
                    if getattr(m, "role", "") == "assistant" and text:
                        last_text = text
                        break
                if last_text:
                    review = await review_output(
                        provider_obj,
                        task_name=str(t.get("name") or ""),
                        task_prompt=str(t.get("prompt") or ""),
                        result=last_text,
                        denied=denied,
                    )
                    if review.get("in_tokens") or review.get("out_tokens"):
                        try:
                            await store.add_usage(
                                sid,
                                getattr(provider_obj, "name", "") or name,
                                getattr(provider_obj, "model", ""),
                                int(review.get("in_tokens") or 0),
                                int(review.get("out_tokens") or 0),
                                int(review.get("cached_tokens") or 0),
                            )
                        except Exception:  # noqa: BLE001 - 用量是附加信息
                            pass
            if error is not None or stop_reason not in ("end_turn", "max_iterations"):
                # 异常结束按失败收尾：run_cron_task_core 回写错误状态、渠道必推
                raise RuntimeError(error or f"轮次异常结束（{stop_reason}）")
            facts = {"denied": denied}
            if review is not None:
                facts["review"] = review
            return runtime, sid, False, facts


        async def team_turn(t: dict):
            # 定时团队二期（团队模式设计 §14.6）：CLI 系统计划任务入口与后端
            # 扫描触发共用 run_cron_team_turn_core——按模板建队、AI 总管自动
            # 闭环，交付说明走 run_cron_task_core 的既有收尾（错误回写 / 渠道
            # 推送 / 运行账本零差异）。装配等价物：模板册与工具表来自上方
            # _assemble_headless_engine；成员 provider 按配置直建（测试经
            # monkeypatch 换 build_provider 桩）。不复用常驻队伍：跨进程无从
            # 核对登记的队伍是否仍在应用内活动（resident_live 缺席），复用会
            # 并行建出第二个编排器写同一 team_id 的频道——缺判定就整段不复用，
            # 每次新建队伍（注册表不动，登记的队伍仍由应用内触发复用）。
            def _release(provs) -> None:
                # 连接池释放入 bg，退出前的 drain_bg_tasks 统一收口
                for prov in provs:
                    aclose = getattr(prov, "aclose", None)
                    if callable(aclose):
                        try:
                            spawn_bg(aclose())
                        except RuntimeError:
                            pass  # 无运行中的事件循环：跳过释放

            def _build_member(name_: str, model_: str | None):
                # 后端 _build_member_provider 的 CLI 等价物：未知服务 raise，
                # 核心降级 build_error 入册（与后端同一容错面）
                if name_ not in cfg.providers:
                    raise ConfigError(
                        "unknown provider: {}; available: {}".format(
                            name_, ", ".join(cfg.providers)))
                pc = cfg.providers[name_]
                if model_:
                    pc = pc.model_copy(update={"model": model_})
                return build_provider(name_, pc, cfg.network)

            return await run_cron_team_turn_core(
                store, t, provider=provider_obj, cfg=cfg,
                cron_pid=int(t["project_id"]), cron_workdir=working_dir,
                template_store=team_templates,
                build_registry=lambda rec: registry,
                build_member_provider=_build_member,
                hooks=hooks, mods=None,
                progress_ledger=None,  # CLI 无工厂观战账本：只计数不发事件
                resident_store=None,   # 不带存活判定 = 不复用（见上）
                release_providers=_release,
                after_usage=None,
            )

        async def one_round(t: dict):
            # mode 分流（与后端 _execute_cron_turn 同款）：团队任务走共享核心
            if str(t.get("mode") or "agent") == "team":
                return await team_turn(t)
            return await agent_turn(t)
        channels = _build_sendonly_channels(cfg)

        final = await run_cron_task_core(
            store, task_id, force=False, execute=one_round,
            broadcast=lambda t: print(f"⏰ {t['name']}：{t['last_status']}"),
            notify=None,  # 无桌面：不打 toast，渠道推送照常
            channels=channels,
            running=None,  # 单次进程，无需在跑去重集合
            # 与后端同一口径的单次运行硬超时（[factory] run_timeout_minutes）
            timeout_s=(
                float(cfg.factory.run_timeout_minutes) * 60.0
                if getattr(cfg.factory, "run_timeout_minutes", 0) else None
            ),
        )
        if final is None:
            # 任务不存在已在上面拦；剩下是「已停用」或「到点槽位已被另一方
            # 认领 / 未到点」（库级 CAS 认领放弃，SEC-M17）——都不算失败，
            # 后者说明本轮已由应用内扫描或另一个子进程接手执行
            print("任务已停用，或本轮已由其他触发方接手（未到点/已被认领），跳过执行")
            return 0
        status = final.get("last_status")
        if status == "error":
            print("✗ " + (final.get("last_result") or "执行失败"))
            return 1
        print(f"✅ {final['name']}：" + ("完成（无产出）" if status == "empty" else "完成"))
        if final.get("last_result"):
            print(final["last_result"])
        return 0
    finally:
        # 终态推送是 fire-and-forget（spawn_bg），而本进程是无人值守推送的
        # 唯一出口：不等落地，asyncio.run 收尾会统一取消在途任务——
        # CancelledError 渠道层接不住、bg 收尾对取消不记日志，推送会静默
        # 丢失且无痕。退出前有界等一轮让在途推送落地（常驻后端无此问题，
        # 事件循环不退出）；超时未归的由 drain 记日志并取消。
        await drain_bg_tasks(timeout=CRON_PUSH_DRAIN_TIMEOUT_S)
        if mcp is not None:
            await mcp.shutdown()
        if tasks is not None:
            tasks.cancel_all()
        await store.close()


def _cron_cmd(args) -> None:
    exit_code = asyncio.run(_cron_run_async(
        args.task_id,
        getattr(args, "provider", None),
        project_dir=getattr(args, "project", None),
    ))
    raise SystemExit(exit_code)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="skysheep", description="SkySheep — open-source AI agent workbench"
    )
    parser.add_argument("--version", action="version", version="skysheep " + __version__)
    sub = parser.add_subparsers(dest="cmd")

    p_chat = sub.add_parser("chat", help="start an interactive coding session")
    p_chat.add_argument("directory", nargs="?", default=".", help="working directory")
    p_chat.add_argument("-p", "--provider", default=None, help="provider name from config")
    p_chat.add_argument("-s", "--session", default=None, help="resume session id")

    p_app = sub.add_parser("app", help="launch the desktop app (native window / browser)")
    p_app.add_argument("directory", nargs="?", default=".", help="working directory")
    p_app.add_argument("-p", "--provider", default=None, help="provider name from config")
    p_app.add_argument("--port", type=int, default=0, help="server port (default: random free port)")
    p_app.add_argument("--browser", action="store_true", help="open in browser instead of native window")

    p_run = sub.add_parser(
        "run", help="headless one-shot: run a task non-interactively (scripts / CI)"
    )
    p_run.add_argument("prompt", help="任务描述（一次性执行后退出）")
    p_run.add_argument("-d", "--directory", default=".", help="working directory")
    p_run.add_argument("-p", "--provider", default=None, help="provider name from config")
    p_run.add_argument(
        "--allow-tool", default="",
        help="预授权工具（逗号分隔，如 write_file,edit_file,run_command）；缺省只放行只读操作",
    )
    p_run.add_argument("--output", choices=["text", "json", "stream"], default="text",
                       help="text=只打印最终回答；json=输出完整结构化结果；"
                            "stream=NDJSON 事件流（每行一个事件、末行 kind=result，"
                            "协议见 docs/无头运行协议.md）")
    p_run.add_argument("--max-iterations", type=int, default=0,
                       help="最大工具调用轮数（0 = 不限制；缺省用 config.toml 的 max_iterations）")
    p_run.add_argument(
        "--trust-project", action="store_true",
        help="信任该项目自带的 .skysheep/ 配置（启用其 mcp.json 与项目级技能）；"
             "无人值守场景没有界面可确认，故默认不启用，需显式指定",
    )

    p_cfg = sub.add_parser("config", help="config operations")
    p_cfg.add_argument("action", choices=["init", "path"], help="init: create template; path: show path")

    p_cron = sub.add_parser(
        "cron", help="定时任务运维（供系统计划任务 / 脚本调用，不启动应用）"
    )
    cron_sub = p_cron.add_subparsers(dest="cron_cmd", required=True)
    p_cron_run = cron_sub.add_parser(
        "run", help="立即执行一个定时任务后退出（Windows 任务计划程序导出的入口）"
    )
    p_cron_run.add_argument("task_id", type=int, help="定时任务 id（应用内任务列表 / cron.list 可查）")
    p_cron_run.add_argument("-p", "--provider", default=None, help="provider name from config")

    p_cronrun = sub.add_parser(
        "cron-run",
        help="立即执行一个定时任务后退出（系统级定时调度入口，任务计划程序到点拉起）",
    )
    p_cronrun.add_argument("task_id", type=int, help="定时任务 id（应用内任务列表 / cron.list 可查）")
    p_cronrun.add_argument(
        "--project", default=None,
        help="项目目录（注册系统计划任务时写入，缺省按任务归属项目从库里解析）",
    )

    sub.add_parser("sessions", help="list recent sessions")
    sub.add_parser("models", help="list configured providers")

    args = parser.parse_args(argv)
    # 注意：Windows 上不强制 Selector 事件循环——MCP stdio 子进程需要 Proactor
    # 写者租约（审计 L26）：命令行会话 / headless run / cron-run 都会持续写会话库，
    # 整段持租约，桌面端的备份/恢复就能看见「别的进程正在写」并在动手前拒绝——
    # 否则替换窗口里那些提交会随旧 inode 一起丢掉。拿不到也照跑（租约不阻塞自己）。
    if args.cmd == "chat":
        with WriterLease():
            asyncio.run(_chat_cmd(args))
    elif args.cmd == "app":
        _app_cmd(args)
    elif args.cmd == "run":
        with WriterLease():
            _run_cmd(args)
    elif args.cmd == "config":
        if args.action == "path":
            print(config_path())
        else:
            try:
                p = write_config_template()
                print("created: " + str(p))
            except ConfigError as e:
                print(str(e))
    elif args.cmd == "cron":
        with WriterLease():
            _cron_cmd(args)
    elif args.cmd == "cron-run":
        with WriterLease():
            _cron_cmd(args)
    elif args.cmd == "sessions":
        asyncio.run(_sessions_cmd())
    elif args.cmd == "models":
        asyncio.run(_models_cmd())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
