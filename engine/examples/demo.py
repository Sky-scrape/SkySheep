"""端到端演示：脚本化模型跑完一个真实的多步编码任务（无需 API Key）。

场景：用户要求写一个除法计算器 → Agent 写文件（权限确认）→ 运行测试
（命令执行需确认，选择"本项目总是允许"）→ 发现 ZeroDivisionError →
修复 → 复测通过（命令已被白名单放行）→ 总结。

运行：uv run python examples/demo.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from rich.console import Console

from skysheep.cli.render import Renderer
from skysheep.core import Agent, build_system_prompt
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import Decision, PermissionGate
from skysheep.tools import RunCommandTool, ToolRegistry, default_tools

CALC_BUGGY = '''"""一个会崩的计算器"""


def safe_div(a, b):
    return a / b


if __name__ == "__main__":
    print("10 / 2 =", safe_div(10, 2))
    print("10 / 0 =", safe_div(10, 0))
'''

CALC_FIXED = '''"""一个会崩的计算器"""


def safe_div(a, b):
    if b == 0:
        return "inf"
    return a / b


if __name__ == "__main__":
    print("10 / 2 =", safe_div(10, 2))
    print("10 / 0 =", safe_div(10, 0))
'''

SCRIPT = [
    # 1) 写出带 bug 的文件
    [
        TextBlock(text="好的，我先创建计算器模块，故意包含一个除零隐患，稍后演示修复流程。"),
        ToolUseBlock(id="c1", name="write_file", input={"path": "calc.py", "content": CALC_BUGGY}),
    ],
    # 2) 运行它 → 崩溃
    [
        TextBlock(text="文件已创建，现在运行验证："),
        ToolUseBlock(id="c2", name="run_command", input={"command": "python calc.py"}),
    ],
    # 3) 修复
    [
        TextBlock(text="运行报了 ZeroDivisionError，问题在 `safe_div` 没有处理 b=0。现在修复："),
        ToolUseBlock(
            id="c3",
            name="edit_file",
            input={
                "path": "calc.py",
                "old_string": "def safe_div(a, b):\n    return a / b",
                "new_string": 'def safe_div(a, b):\n    if b == 0:\n        return "inf"\n    return a / b',
            },
        ),
    ],
    # 4) 复测（这条 run_command 应被白名单自动放行）
    [
        TextBlock(text="修复完成，再跑一次确认："),
        ToolUseBlock(id="c4", name="run_command", input={"command": "python calc.py"}),
    ],
    # 5) 总结
    [
        TextBlock(
            text="✅ 任务完成：`calc.py` 已创建并修复。初次运行暴露 `ZeroDivisionError`，"
            "已让 `safe_div` 在除数为 0 时返回 `\"inf\"`，复测通过（10/2=5.0，10/0=inf）。"
        )
    ],
]


async def main() -> None:
    console = Console()
    demo_dir = Path(tempfile.mkdtemp(prefix="skysheep-demo-"))
    console.print("[bold cyan]SkySheep 端到端演示[/] · 工作目录: " + str(demo_dir) + "\n")

    provider = FakeProvider(SCRIPT)
    gate = PermissionGate()
    agent = Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=gate,
        working_dir=demo_dir,
        max_iterations=10,
    )
    agent.set_system(build_system_prompt(demo_dir))

    renderer = Renderer(console)
    async for ev in agent.run_turn("帮我写一个除法计算器，跑通并确保不崩。"):
        renderer.handle(ev)
        if ev.kind == "permission_request":
            # 模拟用户决策：命令类操作选"本项目总是允许"，其余"允许一次"
            decision = Decision.ALLOW_ALWAYS if ev.tool_name == "run_command" else Decision.ALLOW_ONCE
            agent.respond_permission(ev.request_id, decision)

    console.print("\n[bold]验证:[/]", end=" ")
    content = (demo_dir / "calc.py").read_text(encoding="utf-8")
    assert "if b == 0" in content, "修复未落盘"
    console.print("calc.py 修复已落盘 ✓", end="  ")
    assert gate._match(RunCommandTool(), "python calc.py"), "白名单未生效"
    console.print("run_command 白名单生效 ✓", end="  ")
    assert agent.history[-1].role == "assistant"
    console.print(
        f"历史完整（{len(agent.history)} 条消息, {len(provider.calls)} 次模型调用）✓"
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
