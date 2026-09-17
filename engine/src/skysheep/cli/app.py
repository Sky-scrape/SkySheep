"""SkySheep CLI：终端 REPL + 桌面启动器。

用法：
    skysheep chat [目录] [-p provider] [-s session_id]
    skysheep app [目录] [--port N] [--browser]
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
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console

from .. import __version__
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
from ..core.subagent import CheckTaskTool, SpawnAgentTool, TaskManager
from ..events import ErrorEvent, TextDelta, TurnFinished
from ..mcp import MCPManager, load_mcp_configs
from ..messages import Message
from ..models import Provider
from ..models.factory import build_provider
from ..models.probe import OLLAMA_BASE, probe_ollama
from ..security.gate import Decision, HeadlessGate
from ..security.trust import WorkspaceTrust
from ..session import SessionStore, export_messages_text
from ..skills import SkillLoader
from ..tools import ToolRegistry, default_tools
from ..tools.memory import render_memory_section
from ..tools.skill import LoadSkillTool
from .render import Renderer

console = Console()

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
        self.console = console
        self.renderer = Renderer(console)
        self.store: SessionStore | None = None
        self.project = None
        self.session = None
        self.cfg: SkySheepConfig | None = None
        self.provider: Provider | None = None
        self.provider_name = ""
        self.agent: Agent | None = None
        self.gate = None
        self.prompt: PromptSession | None = None
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
        console.print(
            "[yellow]这个项目自带了会被自动执行的配置：[/yellow]"
        )
        for item in state.get("items", []):
            label = "MCP 服务器" if item.get("kind") == "mcp" else "技能"
            ro = "（其工具自动放行）" if item.get("readonly") else ""
            console.print(f"  · {label} [bold]{item.get('name')}[/bold]：{item.get('detail')}{ro}")
        if state.get("changed"):
            console.print("[yellow]（之前信任过，但配置已变更，需要重新确认）[/yellow]")
        console.print(
            "先不信任也可以照常使用 Agent，只是这些项目自带配置本次不生效。"
        )
        try:
            answer = input("是否信任并启用？（y/N） ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]已跳过（本次不启用项目自带配置）[/dim]")
            return False
        if answer in ("y", "yes"):
            trust.grant()
            console.print("[green]已信任：项目自带配置已启用[/green]")
            return True
        console.print("[dim]未信任：项目自带配置本次不生效（下次打开还会问）[/dim]")
        return False

    async def setup(self) -> None:
        if not self.working_dir.exists():
            raise SystemExit("directory not found: " + str(self.working_dir))
        self.cfg = load_config()
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
        mcp_configs = load_mcp_configs(
            skysheep_home() / "mcp.json",
            (self.working_dir / ".skysheep" / "mcp.json") if self.trusted else None,
        )
        self.mcp = MCPManager(mcp_configs)
        # connect_all 返回工具列表（单值）；连接失败的状态从 statuses 取
        mcp_tools = await self.mcp.connect_all()
        mcp_warnings = [
            f"{n}: {st.error}" for n, st in self.mcp.statuses.items() if st.error
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
        )

        registry = ToolRegistry(default_tools(
            store=self.store,
            websearch=resolve_websearch(self.cfg),
            imagegen=resolve_imagegen(self.cfg),
            computer_control=self.cfg.computer_control,
            browser_control=self.cfg.browser_control,
        ))
        registry.register(LoadSkillTool(self.skills))
        registry.register(SpawnAgentTool(self.tasks))
        registry.register(CheckTaskTool(self.tasks))
        for t in mcp_tools:
            registry.register(t)

        raw_cfg = load_raw_config()
        pre_rules, post_rules = hooks_from_config(raw_cfg)
        hooks = HookRunner(pre_rules, post_rules, working_dir=self.working_dir) \
            if (pre_rules or post_rules) else None

        self.agent = Agent(
            provider=self.provider,
            registry=registry,
            gate=self.gate,
            working_dir=self.working_dir,
            max_iterations=self.cfg.max_iterations,
            context_limit_tokens=self.cfg.context_limit_tokens,
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            hooks=hooks,
        )
        self.agent.set_system(self._compose_system())

        history_file = config_path().parent / "cli-history"
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

        return PermissionGate(store=self.store, project_id=self.project.id)

    def _build_provider(self, name: str):
        if name not in self.cfg.providers:
            raise SystemExit(
                "unknown provider: {}\navailable: {}".format(name, ", ".join(self.cfg.providers))
            )
        try:
            return build_provider(name, self.cfg.providers[name]), name
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
                "[y] 允许一次  [a] 本项目总是允许  [n] 拒绝 > "
            )
        except (EOFError, KeyboardInterrupt):
            return Decision.DENY
        return DECISION_MAP.get(raw.strip().lower(), Decision.DENY)

    # ---- 主循环 ----

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
            interrupted = False
            try:
                async for ev in self.agent.run_turn(text):
                    self.renderer.handle(ev)
                    if ev.kind == "permission_request":
                        decision = await self._ask_permission(ev)
                        self.agent.respond_permission(ev.request_id, decision)
            except KeyboardInterrupt:
                interrupted = True
                self.renderer.close()
                self.console.print("\n[yellow]⏹ 已打断[/]")
            if interrupted:
                await self.store.touch(self.session.id)
                continue
            # 持久化本轮新增消息
            for m in self.agent.history[n_before:]:
                await self.store.append_message(self.session.id, m)
            await self.store.touch(self.session.id)
        self.renderer.close()
        await self.store.close()

    # ---- 斜杠命令 ----

    def _cmd_mcp(self) -> None:
        if not self.mcp or not self.mcp.statuses:
            self.console.print("[dim](未配置 MCP 服务器；参考 ~/.skysheep/mcp.json)[/]")
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
            hint = "(无技能；把 SKILL.md 放到 ~/.skysheep/skills/<名称>/ 或项目的 .skysheep/skills/<名称>/)"
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
            path = Path(arg) if arg else self.working_dir / "skysheep-session.md"
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
    print("已配置 providers（~/.skysheep/config.toml）:")
    for name, pc in cfg.providers.items():
        mark = "→" if name == cfg.default else " "
        print(f"  {mark} {name:<12} {pc.kind:<9} {pc.model}")
    print()
    print(f"本地 Ollama 探测（{OLLAMA_BASE}）:")
    models = await probe_ollama()
    if not models:
        print("  (未检测到 Ollama 服务或模型；启动 ollama serve 并 ollama pull 模型后可用)")
    for m in models:
        print(f"  · {m}")
    if models:
        print()
        print("接入方式：在 config.toml 的 [providers.ollama] 中把 model 改为上面的名称之一。")


def _wait_port(port: int, timeout_s: float = 20.0) -> bool:
    """轮询本机 TCP 端口直到可连接（服务就绪）。主机固定为环回，不构造 URL。"""
    import socket
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.2)
        finally:
            s.close()
    return False


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
    if getattr(args, "port", 0):
        port = args.port
    else:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

    fast_app = create_app(working_dir=args.directory or ".", provider_name=args.provider)
    server = uvicorn.Server(
        uvicorn.Config(fast_app, host=host, port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    if not _wait_port(port):
        raise SystemExit("服务启动失败")
    # 桌面窗口/浏览器兜底一律加载本机回环地址：0.0.0.0 只是绑定地址不是可访问地址，
    # 且守卫对回环永远免令牌，本机界面不受局域网/远程访问开关影响
    url = f"http://127.0.0.1:{port}/"
    console.print(f"[bold cyan]SkySheep[/] 服务已启动: {url}")
    if cfg.server.lan:
        console.print(
            "[yellow]局域网访问已开启：其他设备请用 "
            f"http://<本机IP>:{port}/?token=你的令牌 访问（令牌见 设置 · 手机控制）[/]"
        )
    if cfg.server.tailscale:
        console.print(
            "[yellow]远程访问已开启（Tailscale）：手机登录同一 Tailscale 账号后，"
            f"在任意网络用 http://<电脑的Tailscale地址>:{port}/?token=你的令牌 访问"
            "（地址与二维码见 设置 · 手机控制）[/]"
        )
    return server, url


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
        window.load_html(error_html(detail, str(skysheep_home() / "logs" / "desktop.log")))
    except Exception:
        pass


def _app_cmd(args) -> None:
    """桌面模式：本地服务 + pywebview 原生窗口（浏览器兜底）。"""
    import time
    import webbrowser

    import webview

    from .. import wintheme
    from ..server.app import STATIC_DIR

    use_browser = getattr(args, "browser", False)
    if not use_browser:
        try:
            import webview  # pywebview
        except Exception as e:
            console.print(f"[yellow]pywebview 不可用（{e}），改用浏览器打开[/]")
            use_browser = True

    if use_browser:
        server, url = _start_backend(args)
        webbrowser.open(url)
        console.print("[dim]浏览器模式：Ctrl-C 退出[/]")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return

    # 窗口模式（终端直接 skysheep app）：主窗口第一页是启动动画，引擎就绪后原地切换
    icon_path = STATIC_DIR / "skysheep.ico"
    from ..server.picker import FilePicker

    try:
        screen = webview.screens[0]
    except Exception:
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
    window = webview.create_window(
        "SkySheep",
        html=splash_html(STATIC_DIR),
        width=width,
        height=height,
        min_size=(980, 640),
        js_api=picker,
        x=x,
        y=y,
        # 窗口底色跟主题：默认白底在整页刷新（切项目）/首帧瞬间会露出来
        background_color=wintheme.window_background(wintheme.read_ui_theme()),
    )
    picker.attach(window)
    from ..wintheme import hook_caption_theme, register_window

    hook_caption_theme(window)  # 标题栏染成纸墨主题色（老系统自动跳过）
    register_window(window)  # 前端切主题时事件驱动重刷标题栏（与 desktop.py 同机制）

    def _bootstrap() -> None:
        """GUI 循环启动后执行：后台起引擎与服务，就绪后本窗口内切换到应用页。"""
        try:
            server, url = _start_backend(args)
        except BaseException as exc:  # noqa: BLE001  就地显示失败页；关闭窗口后外层再上报
            errors.append(exc)
            _show_error_page(window, exc)
            return
        try:
            window.load_url(url)
        except Exception:  # noqa: BLE001  窗口被用户提前关掉：无处可切，静默收场
            pass
        running["server"] = server

    errors: list[BaseException] = []
    running: dict[str, object] = {}
    webview.start(_bootstrap, icon=str(icon_path) if icon_path.exists() else None)
    if errors:
        raise errors[0]
    server = running.get("server")
    if server is not None:
        server.should_exit = True


# ---- headless 一次性运行（对标 claude -p / codex exec / gemini -p） ----


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
    其余自动拒绝），消息落库可审计。返回结构化结果；失败抛 SystemExit(2)。"""
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
            provider_obj = build_provider(name, cfg.providers[name])
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

        skills = SkillLoader(
            global_dir=skysheep_home() / "skills",
            project_dir=(working_dir / ".skysheep" / "skills") if trusted else None,
            state_path=working_dir / ".skysheep" / "skills.json",
            scope_path=skysheep_home() / "skills-scope.json",
            project_root=working_dir,
        )
        skills.discover()

        mcp_configs = load_mcp_configs(
            skysheep_home() / "mcp.json",
            (working_dir / ".skysheep" / "mcp.json") if trusted else None,
        )
        mcp = MCPManager(mcp_configs)
        mcp_tools = await mcp.connect_all()

        tasks = TaskManager(
            provider_factory=lambda: provider_obj,
            working_dir=working_dir,
            max_iterations=cfg.subagent_max_iterations,
        )
        registry = ToolRegistry(default_tools(
            store=store,
            websearch=resolve_websearch(cfg),
            imagegen=resolve_imagegen(cfg),
            computer_control=cfg.computer_control,
            browser_control=cfg.browser_control,
        ))
        registry.register(LoadSkillTool(skills))
        registry.register(SpawnAgentTool(tasks))
        registry.register(CheckTaskTool(tasks))
        for t in mcp_tools:
            registry.register(t)

        raw_cfg = load_raw_config()
        pre_rules, post_rules = hooks_from_config(raw_cfg)
        hooks = HookRunner(pre_rules, post_rules, working_dir=working_dir) \
            if (pre_rules or post_rules) else None

        agent = Agent(
            provider=provider_obj,
            registry=registry,
            gate=gate,
            working_dir=working_dir,
            max_iterations=max_iterations or cfg.max_iterations,
            context_limit_tokens=cfg.context_limit_tokens,
            compaction_keep_recent=cfg.compaction_keep_recent,
            hooks=hooks,
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
        async for ev in agent.run_turn(prompt):
            if isinstance(ev, TextDelta) and output == "text":
                print(ev.text, end="", flush=True)
            elif isinstance(ev, ErrorEvent):
                error = ev.message
            elif isinstance(ev, TurnFinished):
                stop_reason = ev.stop_reason
        if output == "text" and stop_reason != "max_iterations":
            print()  # 流式文本后补换行

        new_msgs = agent.history[n_before:]
        for m in new_msgs:
            await store.append_message(sess.id, m)
        await store.touch(sess.id)
        reply = next(
            (m.text for m in reversed(new_msgs)
             if m.role == "assistant" and m.text.strip()),
            "",
        )
        return {
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
    else:
        if result["reply"]:
            print(result["reply"])
        if result.get("error"):
            console.print("[red]✗ " + result["error"] + "[/]")
    # 退出码：正常收尾 0；出错/被打断/异常中断 1 —— 方便脚本与 CI 判断
    if result.get("error") or result["stop_reason"] != "end_turn":
        raise SystemExit(1)


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
    p_run.add_argument("--output", choices=["text", "json"], default="text",
                       help="text=只打印最终回答；json=输出完整结构化结果")
    p_run.add_argument("--max-iterations", type=int, default=0,
                       help="最大工具调用轮数（缺省用 config.toml 的 max_iterations）")
    p_run.add_argument(
        "--trust-project", action="store_true",
        help="信任该项目自带的 .skysheep/ 配置（启用其 mcp.json 与项目级技能）；"
             "无人值守场景没有界面可确认，故默认不启用，需显式指定",
    )

    p_cfg = sub.add_parser("config", help="config operations")
    p_cfg.add_argument("action", choices=["init", "path"], help="init: create template; path: show path")

    sub.add_parser("sessions", help="list recent sessions")
    sub.add_parser("models", help="list configured providers and local (Ollama) models")

    args = parser.parse_args(argv)
    # 注意：Windows 上不强制 Selector 事件循环——MCP stdio 子进程需要 Proactor
    if args.cmd == "chat":
        asyncio.run(_chat_cmd(args))
    elif args.cmd == "app":
        _app_cmd(args)
    elif args.cmd == "run":
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
    elif args.cmd == "sessions":
        asyncio.run(_sessions_cmd())
    elif args.cmd == "models":
        asyncio.run(_models_cmd())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
