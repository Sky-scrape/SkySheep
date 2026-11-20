"""子代理工具的统一装配点。

CLI（REPL 与无头 run）与 server 原先各写一份子代理工具注册清单，结果 CLI
漏了 wait_task（只有 spawn_agent / check_task）。三件套的清单收在这里，
两侧统一调 register_subagent_tools，新增/调整子代理工具只改一处。

导入位置说明：本模块从 core.subagent 取工具类，而 core.subagent 会导入
tools 包（只用包内已有的工具类）。tools/__init__.py 不导入本模块，因此
运行期不存在循环导入；不要在 tools/__init__.py 里 re-export 这里。
"""

from __future__ import annotations

from ..core.subagent import CheckTaskTool, SpawnAgentTool, WaitTaskTool
from .base import Tool, ToolRegistry

# 子代理工具清单：注册顺序即模型看到的工具顺序，三件套必须齐
SUBAGENT_TOOL_FACTORIES: tuple[type[Tool], ...] = (
    SpawnAgentTool,
    CheckTaskTool,
    WaitTaskTool,
)


def register_subagent_tools(
    registry: ToolRegistry, tasks: object | None, enabled: bool
) -> None:
    """按统一清单注册子代理三件套（spawn_agent / check_task / wait_task）。

    子代理要工作目录才能跑：没有任务簿（tasks 为 None）或设置页整体关闭
    （enabled=False）时不注册，模型看不到这些工具。工具构造只存任务簿引用，
    这里不做任何 IO。
    """
    if not enabled or tasks is None:
        return
    for factory in SUBAGENT_TOOL_FACTORIES:
        registry.register(factory(tasks))
