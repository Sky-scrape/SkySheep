"""MCP 工具注解（annotations 四布尔）测试。

每个内置工具必须在 to_schema() 导出 readOnlyHint / destructiveHint /
idempotentHint / openWorldHint 且全为布尔——外部宿主与工具目录（M8ven、
OpenAI directory 等）按它在调用前分级提示，缺键或非布尔会被目录拒收。
这里把全部内置工具逐个点名，顺带保证每个工具都有测试引用。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from skysheep.core.subagent import CheckTaskTool, SpawnAgentTool
from skysheep.tools import ToolRegistry, default_tools
from skysheep.tools.base import Tool
from skysheep.tools.pipeline import PipelineWriteTool
from skysheep.tools.schedule import ScheduleWriteTool
from skysheep.tools.skill import LoadSkillTool

ANNOTATION_KEYS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}

# default_tools() 的默认全集（computer/browser 控制默认开；store 不传无 schedule_write）
DEFAULT_TOOLS = [
    "read_file",
    "read_image",
    "write_file",
    "edit_file",
    "move_file",
    "delete_file",
    "make_dir",
    "list_dir",
    "glob",
    "grep",
    "run_command",
    "todo_write",
    "web_fetch",
    "web_search",
    "read_document",
    "write_document",
    "memory_write",
    "generate_image",
    "screenshot",
    "window_list",
    "clipboard_read",
    "clipboard_write",
    "mouse",
    "keyboard",
    "window",
    "browser",
]

# server 端按需装配、不在 default_tools() 默认集里的工具
CONTEXTUAL_TOOLS = [
    "schedule_write",
    "load_skill",
    "pipeline_write",
    "spawn_agent",
    "check_task",
]


def _build(name: str) -> Tool:
    if name == "schedule_write":
        return ScheduleWriteTool(store=None)
    if name == "load_skill":
        return LoadSkillTool(loader=None)
    if name == "pipeline_write":
        return PipelineWriteTool(store=None, project_id_fn=lambda: 0)
    if name == "spawn_agent":
        tasks = type("_Tasks", (), {"list_custom": staticmethod(lambda: [])})()
        return SpawnAgentTool(tasks)
    if name == "check_task":
        return CheckTaskTool(None)
    return ToolRegistry(default_tools()).get(name)


def _annotations(name: str) -> dict:
    schema = _build(name).to_schema()
    assert set(schema) == {"name", "description", "input_schema", "annotations"}
    return schema["annotations"]


def test_default_registry_has_exactly_the_documented_tools():
    registry = ToolRegistry(default_tools())
    assert sorted(t.name for t in registry.all()) == sorted(DEFAULT_TOOLS)


@pytest.mark.parametrize("name", DEFAULT_TOOLS + CONTEXTUAL_TOOLS)
def test_every_tool_declares_four_boolean_hints(name):
    ann = _annotations(name)
    assert set(ann) == ANNOTATION_KEYS
    assert all(isinstance(v, bool) for v in ann.values()), name


@pytest.mark.parametrize(
    "name,ro,dest,idem,world",
    [
        ("read_file", True, False, True, False),
        ("make_dir", False, False, True, False),
        ("delete_file", False, True, False, False),
        ("run_command", False, True, False, True),
        ("web_fetch", True, False, True, True),
        ("browser", False, False, False, True),
        ("mouse", False, True, False, False),
        ("memory_write", False, False, False, False),
        ("clipboard_read", True, False, True, False),
        ("spawn_agent", False, False, False, False),
        ("check_task", True, False, True, False),
    ],
)
def test_annotation_values_match_tool_semantics(name, ro, dest, idem, world):
    assert _annotations(name) == {
        "readOnlyHint": ro,
        "destructiveHint": dest,
        "idempotentHint": idem,
        "openWorldHint": world,
    }


def test_base_defaults_are_conservative_for_remote_mcp_tools():
    """接入的远程 MCP 工具不声明注解时落到基类缺省：宁可疑其有写、有破坏性。"""

    class _RemoteArgs(BaseModel):
        x: int = 1

    class _Remote(Tool):
        name = "remote_stub"
        description = "stub"
        args_model = _RemoteArgs

        async def run(self, args, ctx):  # pragma: no cover
            return ""

    schema = _Remote().to_schema()
    assert schema["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
