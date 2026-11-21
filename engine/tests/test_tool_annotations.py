"""MCP 工具注解（annotations 四布尔）测试。

每个内置工具必须在 to_schema() 导出 readOnlyHint / destructiveHint /
idempotentHint / openWorldHint 且全为布尔——外部宿主与工具目录（M8ven、
OpenAI directory 等）按它在调用前分级提示，缺键或非布尔会被目录拒收。
这里把全部内置工具逐个点名，顺带保证每个工具都有测试引用。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from skysheep.core.mods_tool import ModTool
from skysheep.core.subagent import CheckTaskTool, SpawnAgentTool, WaitTaskTool
from skysheep.tools import ToolRegistry, default_tools
from skysheep.tools.base import Safety, Tool
from skysheep.tools.history import SearchHistoryTool
from skysheep.tools.pipeline import PipelineWriteTool
from skysheep.tools.schedule import ScheduleWriteTool
from skysheep.tools.skill import LoadSkillTool

ANNOTATION_KEYS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}

# default_tools() 显式打开 computer/browser 控制时的全集
# （store 不传无 schedule_write / search_history，两者都在 store 分支注册）
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
    "index_documents",
    "search_documents",
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
    "web_page",
]

# 电脑控制七件套 + 浏览器控制（browser + web_page 页内自动化）：形参默认关
# （安全审查 M16），要显式打开才注册
CONTROL_TOOLS = [
    "screenshot",
    "window_list",
    "clipboard_read",
    "clipboard_write",
    "mouse",
    "keyboard",
    "window",
    "browser",
    "web_page",
]

# server 端按需装配、不在 default_tools() 默认集里的工具
# （schedule_write / search_history 都在 default_tools 的 store 分支注册）
CONTEXTUAL_TOOLS = [
    "schedule_write",
    "search_history",
    "load_skill",
    "pipeline_write",
    "spawn_agent",
    "check_task",
    "wait_task",
]

# safety（本地权限门的放行策略）与 read_only_hint（对外宿主/工具目录在调用前
# 的分级提示）是两个不同轴，通常同向（只读工具两边都「不改环境」，写/危险
# 工具两边都「有写动作」）。个别工具有意让两者方向相反，每条理由与工具定义
# 处的代码注释一致；契约测试（test_annotation_mismatch_must_be_whitelisted）
# 遍历全部内置工具，出现任何不在此表的新不一致一律红；白名单里的陈旧条目
# （工具已改注解、不再不一致）也会被 test_intentional_mismatch_whitelist_
# has_no_stale_entry 揪出来，防止这份表烂掉。
INTENTIONAL_ANNOTATION_MISMATCHES: dict[str, str] = {
    "clipboard_read": (
        "读操作本身不改环境，故 read_only_hint=True；剪贴板常驻密码管理器刚复制的"
        "凭据等敏感内容，须与 clipboard_write 同一确认姿态，故 safety=WRITE（安全审查 D4）。"
    ),
    "todo_write": (
        "待办清单只存引擎内存态、不触工作区文件，故 safety=READONLY 自动放行；"
        "每次调用全量覆盖清单、对环境有写动作，故 read_only_hint=False"
        "（同参数重复写入结果一致，同时 idempotent_hint=True）。"
    ),
    "memory_write": (
        "只写 SkySheep 自有记忆文件、没有任意路径写面，故 safety=READONLY；"
        "落点在引擎主目录 ~/.skysheep/memory.md 且注入所有会话 system prompt、"
        "append/delete 走逐次确认（审查 FINDING 2），对宿主按有写动作标注 read_only_hint=False。"
    ),
    "schedule_write": (
        "只写 SkySheep 自有日程库、safety=READONLY 免权限确认；"
        "add/update/delete 对环境有写动作，故 read_only_hint=False 如实标注。"
    ),
    "pipeline_write": (
        "只写 SkySheep 自有流水线库、safety=READONLY 免权限确认；"
        "create/update_node 等对环境有写动作，故 read_only_hint=False 如实标注。"
    ),
    "spawn_agent": (
        "派生本身只是登记子任务（子代理内的写/命令操作会被 SubagentGate 自动拒绝），"
        "故 safety=READONLY；派生的子代理会真实执行任务，对外按「有写动作」如实标注"
        " read_only_hint=False。"
    ),
}


def _build(name: str) -> Tool:
    if name == "schedule_write":
        return ScheduleWriteTool(store=None)
    if name == "search_history":
        return SearchHistoryTool(store=None)
    if name == "load_skill":
        return LoadSkillTool(loader=None)
    if name == "pipeline_write":
        return PipelineWriteTool(store=None, project_id_fn=lambda: 0)
    if name == "spawn_agent":
        tasks = type(
            "_Tasks",
            (),
            {
                "list_custom": staticmethod(lambda: []),
                "list_builtin_desc": staticmethod(lambda: [("researcher", "调研")]),
            },
        )()
        return SpawnAgentTool(tasks)
    if name == "check_task":
        return CheckTaskTool(None)
    if name == "wait_task":
        return WaitTaskTool(None)
    # 注解抽查要覆盖电脑/浏览器控制工具，它们默认关，这里显式打开
    return ToolRegistry(
        default_tools(computer_control=True, browser_control=True)
    ).get(name)


def _annotations(name: str) -> dict:
    schema = _build(name).to_schema()
    assert set(schema) == {"name", "description", "input_schema", "annotations"}
    return schema["annotations"]


def _annotation_mismatch(tool: Tool) -> bool:
    """safety 与 read_only_hint 是否方向相反（两种形态之一）：

    - safety=READONLY（本地权限门按只读自动放行）但 read_only_hint=False
      （对外宿主按「有写动作」提示）；
    - safety 非 READONLY（本地默认需确认）但 read_only_hint=True
      （对外宿主按「不改环境」提示）。
    """
    return (tool.safety == Safety.READONLY) != bool(tool.read_only_hint)


def test_default_registry_has_exactly_the_documented_tools():
    registry = ToolRegistry(default_tools(computer_control=True, browser_control=True))
    assert sorted(t.name for t in registry.all()) == sorted(DEFAULT_TOOLS)


def test_control_tools_are_off_by_default():
    """M16：形参默认关——新调用方漏传不会静默打开截屏/键鼠/剪贴板与浏览器控制。

    默认值必须与 config.computer_control / browser_control 同一口径（都是 False），
    否则「配置里默认关、工具集里默认开」这种矛盾迟早会变成真实的攻击面。
    """
    from skysheep.config import SkySheepConfig

    names = {t.name for t in default_tools()}
    assert not (names & set(CONTROL_TOOLS)), names & set(CONTROL_TOOLS)
    assert names == set(DEFAULT_TOOLS) - set(CONTROL_TOOLS)
    cfg = SkySheepConfig()
    assert cfg.computer_control is False and cfg.browser_control is False


@pytest.mark.parametrize("name", DEFAULT_TOOLS + CONTEXTUAL_TOOLS)
def test_every_tool_declares_four_boolean_hints(name):
    ann = _annotations(name)
    assert set(ann) == ANNOTATION_KEYS
    assert all(isinstance(v, bool) for v in ann.values()), name


@pytest.mark.parametrize(
    "name,ro,dest,idem,world",
    [
        ("read_file", True, False, True, False),
        ("search_history", True, False, True, False),
        ("make_dir", False, False, True, False),
        ("delete_file", False, True, False, False),
        ("run_command", False, True, False, True),
        ("web_fetch", True, False, True, True),
        ("browser", False, False, False, True),
        ("web_page", False, True, False, True),
        ("mouse", False, True, False, False),
        ("memory_write", False, False, False, False),
        ("index_documents", False, False, True, False),
        ("search_documents", True, False, True, False),
        ("clipboard_read", True, False, True, False),
        ("spawn_agent", False, False, False, False),
        ("check_task", True, False, True, False),
        ("wait_task", True, False, False, False),
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


def test_mod_tool_exports_annotations_and_preserves_raw_schema():
    raw_schema = {
        "type": "object",
        "properties": {"nested": {"type": "object", "properties": {"value": {"type": "string"}}}},
    }
    tool = ModTool(
        mod_id="test-mod",
        name="mod_tool",
        description="Mod tool",
        schema=raw_schema,
        loaded_mod=object(),
    )

    exported = tool.to_schema()

    assert set(exported) == {"name", "description", "input_schema", "annotations"}
    assert exported["input_schema"] is raw_schema
    assert exported["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert all(isinstance(value, bool) for value in exported["annotations"].values())


@pytest.mark.parametrize("name", DEFAULT_TOOLS + CONTEXTUAL_TOOLS)
def test_annotation_mismatch_must_be_whitelisted(name):
    """机械契约：safety ↔ read_only_hint 方向相反必须是登记过的有意设计。

    两者语义不同轴（safety 管本地权限门，read_only_hint 管对外宿主呈现），
    但新工具默认应同向；出现任何不在 INTENTIONAL_ANNOTATION_MISMATCHES
    白名单里的新不一致一律红。属有意设计就登记理由；属标错只改
    read_only_hint 注解——safety 影响权限门的 UX 行为，不要动。
    """
    tool = _build(name)
    if _annotation_mismatch(tool):
        assert name in INTENTIONAL_ANNOTATION_MISMATCHES, (
            f"{name} 出现新的 safety/read_only_hint 方向不一致"
            f"（safety={tool.safety.value}，read_only_hint={tool.read_only_hint}）。"
            "若是无意标错请改 read_only_hint 注解；若属有意设计请在"
            " INTENTIONAL_ANNOTATION_MISMATCHES 登记理由。"
        )


def test_intentional_mismatch_whitelist_has_no_stale_entry():
    """白名单不许养陈旧条目：工具注解改齐、不再不一致的名字必须移出白名单。

    漏登记的方向会在上面的参数化契约里红，这里反向兜住「条目已过时」，
    两头夹住白名单与真实不一致集合恒等。
    """
    mismatching = {
        t.name
        for t in (_build(name) for name in DEFAULT_TOOLS + CONTEXTUAL_TOOLS)
        if _annotation_mismatch(t)
    }
    stale = sorted(set(INTENTIONAL_ANNOTATION_MISMATCHES) - mismatching)
    assert not stale, f"白名单陈旧条目（这些工具已不再方向不一致，请移除）：{stale}"
