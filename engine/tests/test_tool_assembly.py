# 子代理工具装配的契约测试（返工防线：派生优先于手抄）。
#
# 背景：server/backend.py 与 cli/app.py 原先各写一份子代理工具注册清单，CLI
# 漏了 wait_task——模型只能反复 check_task 轮询，多耗轮次。现在三件套由
# tools/assembly.py 的 SUBAGENT_TOOL_FACTORIES 单一维护，两侧统一调用
# register_subagent_tools。本文件钉住四件事：
# - 清单本身恰为 spawn_agent / check_task / wait_task；
# - enabled / tasks 两个门闩的行为（关则不注册，无任务簿不注册）；
# - 两个宿主源码确实走装配器，不再手写 registry.register(SpawnAgentTool(...))
#   这类平行清单（手写注册只允许留在 assembly.py）；
# - 系统提示词也确实提到了 wait_task。
from __future__ import annotations

from pathlib import Path

from skysheep.tools import ToolRegistry
from skysheep.tools.assembly import SUBAGENT_TOOL_FACTORIES, register_subagent_tools

SRC = Path(__file__).resolve().parents[1] / "src" / "skysheep"
CLI_APP = SRC / "cli" / "app.py"
SERVER_BACKEND = SRC / "server" / "backend.py"
PROMPT = SRC / "core" / "prompt.py"

EXPECTED_NAMES = {"spawn_agent", "check_task", "wait_task"}


class _FakeTasks:
    """任务簿替身：SpawnAgentTool 构造时只读内置/自定义子代理说明。"""

    def list_builtin_desc(self) -> list[tuple[str, str]]:
        return [("explore", "只读调研")]

    def list_custom(self) -> list[tuple[str, str]]:
        return []


def test_subagent_tool_factories_exact_names():
    names = {factory.name for factory in SUBAGENT_TOOL_FACTORIES}
    assert names == EXPECTED_NAMES


def test_register_subagent_tools_respects_switch_and_tasks():
    tasks = _FakeTasks()

    off = ToolRegistry()
    register_subagent_tools(off, tasks, enabled=False)
    assert off.all() == []

    on = ToolRegistry()
    register_subagent_tools(on, tasks, enabled=True)
    assert {tool.name for tool in on.all()} == EXPECTED_NAMES


def test_register_subagent_tools_skips_without_tasks():
    registry = ToolRegistry()
    register_subagent_tools(registry, None, enabled=True)
    assert registry.all() == []


def test_hosts_use_shared_assembly():
    # 两个宿主都必须调用装配器；手写的三件套注册只允许留在 assembly.py，
    # 否则「加一件新工具只改一处」的约定就断了。
    hand_written = (
        "registry.register(SpawnAgentTool(",
        "registry.register(CheckTaskTool(",
        "registry.register(WaitTaskTool(",
    )
    for path in (CLI_APP, SERVER_BACKEND):
        text = path.read_text(encoding="utf-8")
        assert "register_subagent_tools(" in text, f"{path} 未走统一装配"
        for snippet in hand_written:
            assert snippet not in text, f"{path} 仍有手写注册：{snippet}"


def test_prompt_mentions_wait_task():
    assert "wait_task" in PROMPT.read_text(encoding="utf-8")
