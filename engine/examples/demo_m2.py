"""M2 能力验收演示：Skills / 子代理 / 上下文压缩 / 真实 MCP 调用（无需 API Key）。

运行：uv run python examples/demo_m2.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from skysheep.core import Agent
from skysheep.core.context import estimate_tokens
from skysheep.core.prompt import build_system_prompt
from skysheep.core.subagent import SpawnAgentTool, TaskManager
from skysheep.mcp import MCPManager, MCPServerConfig
from skysheep.messages import Message, TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import PermissionGate
from skysheep.skills import SkillLoader
from skysheep.tools import ToolRegistry, default_tools
from skysheep.tools.base import ToolContext
from skysheep.tools.skill import LoadSkillTool

console = Console()
DEMO_DIR = Path(__file__).parent


def banner(title: str) -> None:
    console.print()
    console.print(Panel(title, border_style="cyan"))


# ---------- 1. Skills ----------


async def demo_skills(workdir: Path) -> SkillLoader:
    banner("1) Skills：发现 / 注入 / 渐进式披露")
    skill_dir = workdir / ".skysheep" / "skills" / "commit-helper"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: commit-helper\ndescription: 按约定式提交规范写 commit message\n---\n"
        "规则：\n1. 使用 feat/fix/docs/chore 前缀\n2. 标题不超过 50 字符\n"
        "3. 正文解释 why 而非 what\n",
        encoding="utf-8",
    )
    loader = SkillLoader(
        global_dir=workdir / "no-global",
        project_dir=workdir / ".skysheep" / "skills",
        state_path=workdir / ".skysheep" / "skills.json",
    )
    loader.discover()
    console.print("[green]发现技能:[/]", [(s.name, s.source) for s in loader.all()])
    section = loader.render_prompt_section()
    console.print("[green]注入系统提示词片段:[/]")
    console.print(section)

    tool = LoadSkillTool(loader)
    body = await tool.run(tool.args_model(name="commit-helper"), ToolContext(working_dir=workdir))
    console.print("[green]load_skill 读取正文（前 80 字符）:[/]", body[:80].replace("\n", " "))
    return loader


# ---------- 2. 子代理 ----------


async def demo_subagent(workdir: Path) -> None:
    banner("2) 子代理：主 Agent 派发 explore 调研任务（同步返回报告）")
    (workdir / "calc.py").write_text("def safe_div(a, b):\n    return a / b\n", encoding="utf-8")

    main_script = [
        [ToolUseBlock(id="s1", name="spawn_agent", input={
            "agent_type": "explore",
            "prompt": "调研这个目录里的 Python 文件并总结其功能",
        })],
        [TextBlock(text="主 Agent 已拿到子代理报告，任务完成。")],
    ]
    sub_script = [[TextBlock(text="REPORT: 发现 calc.py，实现 safe_div 除法函数（存在除零隐患）。")]]

    tasks = TaskManager(provider_factory=lambda: FakeProvider(list(sub_script)), working_dir=workdir)
    registry = ToolRegistry(default_tools())
    registry.register(SpawnAgentTool(tasks))
    agent = Agent(
        provider=FakeProvider(main_script),
        registry=registry,
        gate=PermissionGate(),
        working_dir=workdir,
    )
    agent.set_system(build_system_prompt(workdir))
    async for ev in agent.run_turn("派子代理调研一下这个目录"):
        if ev.kind == "tool_call_finished":
            console.print("[green]子代理报告（tool_result 预览）:[/]", ev.preview[:100])


# ---------- 3. 上下文压缩 ----------


async def demo_compaction(workdir: Path) -> None:
    banner("3) 上下文压缩：超过阈值自动摘要替换旧历史")
    provider = FakeProvider([
        [TextBlock(text="摘要：用户在调试除零错误；已定位 calc.py:2；下一步修复。")],
        [TextBlock(text="继续任务。")],
    ])
    agent = Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=PermissionGate(),
        working_dir=workdir,
        context_limit_tokens=10,  # 故意调低触发压缩
        compaction_keep_recent=2,
    )
    agent.set_system("sys")
    filler = "F" * 300
    for i in range(5):
        agent.history.append(Message.user(f"第{i}轮 {filler}"))
        agent.history.append(Message.assistant([TextBlock(text=f"回复{i}")]))
    console.print(f"[green]压缩前:[/] {len(agent.history)} 条消息, ~{estimate_tokens(agent.history)} tokens")
    async for ev in agent.run_turn("继续"):
        if ev.kind == "compaction":
            console.print(f"[green]压缩事件:[/] {ev.before_messages} → {ev.after_messages} 条消息")
            console.print("[green]摘要消息:[/]", agent.history[1].text[:80])
    assert "earlier-conversation-summary" in agent.history[1].text


# ---------- 4. 真实 MCP 调用 ----------


async def demo_mcp(workdir: Path) -> None:
    banner("4) MCP：通过 stdio 连接本地 demo server，走真实协议调用工具")
    manager = MCPManager({
        "demo": MCPServerConfig(command=sys.executable, args=[str(DEMO_DIR / "mcp_demo_server.py")]),
    })
    tools = await manager.connect_all()
    console.print("[green]已注册 MCP 工具:[/]", [t.name for t in tools])
    add_tool = next(t for t in tools if t.name == "mcp__demo__add")
    out = await add_tool.run(add_tool.args_model(a=20, b=22), ToolContext(working_dir=workdir))
    console.print("[green]mcp__demo__add(20, 22) =[/]", out.strip())
    assert "42" in out
    await manager.shutdown()
    console.print("[green]连接已关闭 ✓[/]")


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="skysheep-m2-"))
    console.print("[bold cyan]SkySheep M2 验收演示[/] · 工作目录: " + str(workdir))
    await demo_skills(workdir)
    await demo_subagent(workdir)
    await demo_compaction(workdir)
    await demo_mcp(workdir)
    console.print()
    console.print(Panel("✅ M2 四项能力全部验证通过", border_style="green"))


if __name__ == "__main__":
    asyncio.run(main())
