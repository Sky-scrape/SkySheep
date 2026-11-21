"""_TOOL_SPECS 完备性契约：新增 Tool 子类必须登记，漏登记即红。

背景（独立复核遗留）：_TOOL_SPECS（tools/__init__.py，内置工具的单一清单）
的完备性此前没有契约——test_code_hygiene 只钉「每个工具类声明 safety +
MCP 四注解」，test_tool_annotations 只钉 default_tools() 的运行时集合，
两者都拦不住「新写一个 Tool 子类、忘了在 _TOOL_SPECS 登记」：类不会被
default_tools() 注册，模型看不到，也没有任何测试逼你登记。

本契约用 Tool.__subclasses__() 递归收集全部**具体**工具类，与 _TOOL_SPECS
声明集合两头夹住：

- 漏登记 → 红（提示加 _ToolSpec 条目，或确认属装配点注入并进白名单写实理由）；
- _TOOL_SPECS 僵尸条目（指向抽象类 / 不再是 Tool 具体子类）→ 红；
- 白名单陈旧（类已删除改名 / 已登记进 _TOOL_SPECS）→ 红，白名单不烂。

构造依赖在装配点注入的工具（子代理三件套、load_skill、pipeline_write 等）
不在 default_tools() 能力范围内，属合理例外——白名单逐条写实理由，
新增例外必须显式登记，不许静默放过。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import skysheep.tools as tools_pkg
from skysheep.core.mods_tool import ModTool
from skysheep.core.subagent import CheckTaskTool, SpawnAgentTool, WaitTaskTool
from skysheep.core.team import (
    TeamAcceptTool,
    TeamAssignTool,
    TeamDeliverTool,
    TeamDropTool,
    TeamOpenHuddleTool,
    TeamReassignTool,
    TeamRejectTool,
)
from skysheep.mcp.client import MCPTool
from skysheep.tools import _TOOL_SPECS
from skysheep.tools.base import Tool
from skysheep.tools.fs import ReadFileTool
from skysheep.tools.pipeline import PipelineWriteTool
from skysheep.tools.skill import LoadSkillTool

# ---- 收集：递归走 Tool.__subclasses__() ---------------------------------

def _collect_concrete_tool_subclasses() -> set[type[Tool]]:
    """先导入所有定义工具类的模块，再递归收集 skysheep.* 里的具体 Tool 子类。

    __subclasses__() 只看到「已定义」的类，所以先把定义点都拉进来：
    - tools 包逐模块导入（含 tools/__init__.py 不导入的 pipeline / skill）；
    - core.subagent / core.team / core.mods_tool / mcp.client 显式导入
      （子代理三件套 / 团队总管七件套 / Mod 包装 / MCP 包装）。

    测试与示例模块也会定义 Tool 桩类（如 test_tool_annotations 的 _Remote），
    全量跑套件时同样出现在 __subclasses__() 里——按 __module__ 前缀只保留
    生产代码 skysheep.* 命名空间，收集结果与测试执行顺序无关。
    """
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        try:
            importlib.import_module(f"skysheep.tools.{mod.name}")
        except Exception as exc:  # noqa: BLE001 - 与 test_code_hygiene 同款：可选依赖缺失跳过
            pytest.skip(f"工具模块 {mod.name} 无法导入：{exc}")
    importlib.import_module("skysheep.core.subagent")
    importlib.import_module("skysheep.core.team")
    importlib.import_module("skysheep.core.mods_tool")
    importlib.import_module("skysheep.mcp.client")

    seen: set[type[Tool]] = set()
    stack = list(Tool.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls.__module__.startswith("skysheep."):
            seen.add(cls)
        stack.extend(cls.__subclasses__())
    # 抽象中间基类（如团队总管的 _DirectorToolBase，run 仍未实现）不算工具，
    # 它的注解与登记义务由具体子类承担（test_code_hygiene 同一口径）
    return {cls for cls in seen if not inspect.isabstract(cls)}


# ---- 白名单：装配点注入 / 动态包装，不进 _TOOL_SPECS 的合理例外 -----------
# 按类对象作键：类删除或改名时本文件的 import 先行报错，比名字串更早暴露。

WHITELIST: dict[type[Tool], str] = {
    # 构造注入族：__init__ 需要 default_tools() 不持有的依赖，由持有依赖的
    # 装配点紧随 default_tools() 注册——塞进 _TOOL_SPECS 反而撞重复注册。
    LoadSkillTool: (
        "构造注入 SkillLoader（tools/skill.py __init__(self, loader)）："
        "default_tools() 不持有技能装载器；cli/app.py 与 server/backend.py 的"
        "装配点紧随 default_tools() 注册，进 _TOOL_SPECS 会与其重复注册撞"
        " duplicate tool name ValueError"
    ),
    PipelineWriteTool: (
        "构造注入 SessionStore + 当前项目 id 闭包（tools/pipeline.py "
        "__init__(self, store, project_id_fn, tasks_fn)）：流水线绑定项目，"
        "无项目态不注册（backend.py 两处装配分支），且仅 server 装配、CLI 不注册"
    ),
    SpawnAgentTool: (
        "构造注入 TaskManager（core/subagent.py __init__(self, tasks)）："
        "子代理三件套的单一清单在 tools/assembly.py 的 SUBAGENT_TOOL_FACTORIES，"
        "register_subagent_tools 统一装配（CLI/server 两处），按 subagent_enabled 开关"
    ),
    CheckTaskTool: (
        "构造注入 TaskManager：同 SpawnAgentTool——三件套单一清单在 "
        "tools/assembly.py 的 SUBAGENT_TOOL_FACTORIES"
    ),
    WaitTaskTool: (
        "构造注入 TaskManager：同 SpawnAgentTool——三件套单一清单在 "
        "tools/assembly.py 的 SUBAGENT_TOOL_FACTORIES（历史上 CLI 正是漏了它才收口的）"
    ),
    # 动态包装族：实例按运行期清单逐个构造，无法静态登记。
    ModTool: (
        "动态包装：每个 Mod 注册的工具各建一个实例（backend.py 的 mods 循环），"
        "name/description/schema 全部来自 Mod 运行期声明，没有静态清单可登记"
    ),
    MCPTool: (
        "动态包装：每个 MCP 服务器的每个工具各建一个实例（MCPManager 列出工具时构造），"
        "名字形如 mcp__<server>__<tool>，来自服务器运行期 tools/list"
    ),
    # 团队 AI 总管内部工具：按队构造、只进总管自己的临时注册表。
    TeamAssignTool: (
        "构造注入 TeamOrchestrator（core/team.py 建队时逐队构造）：AI 总管的"
        "内部团队七件套只进总管自己的临时注册表，不在任何 default_tools() 路径上"
    ),
    TeamAcceptTool: (
        "构造注入 TeamOrchestrator：同 TeamAssignTool——总管内部七件套，按队构造"
    ),
    TeamRejectTool: (
        "构造注入 TeamOrchestrator：同 TeamAssignTool——总管内部七件套，按队构造"
    ),
    TeamReassignTool: (
        "构造注入 TeamOrchestrator：同 TeamAssignTool——总管内部七件套，按队构造"
    ),
    TeamDropTool: (
        "构造注入 TeamOrchestrator：同 TeamAssignTool——总管内部七件套，按队构造"
    ),
    TeamOpenHuddleTool: (
        "构造注入 TeamOrchestrator：同 TeamAssignTool——总管内部七件套，按队构造"
    ),
    TeamDeliverTool: (
        "构造注入 TeamOrchestrator：同 TeamAssignTool——总管内部七件套，按队构造"
    ),
}

# 四个哨兵覆盖四种「类进入 __subclasses__」的路径：tools/__init__.py 直接
# 导入的模块（fs）、__init__.py 不导入、靠 pkgutil 补导入的 tools 模块（skill）、
# core 侧显式导入（team）、mcp 侧显式导入（client）。缺任何一个都说明收集
# 断了——完备性检查会在残缺集合上假绿。
_COLLECTION_SENTINELS = (ReadFileTool, LoadSkillTool, TeamAssignTool, MCPTool)


# ---- 契约 ----------------------------------------------------------------

def test_every_concrete_tool_subclass_is_registered_or_whitelisted():
    """全部具体工具类必须登记进 _TOOL_SPECS 或白名单，漏登记即红。"""
    collected = _collect_concrete_tool_subclasses()
    declared = {spec.cls for spec in _TOOL_SPECS}
    missing = sorted(
        f"{cls.__module__}.{cls.__name__}"
        for cls in collected - declared - set(WHITELIST)
    )
    assert not missing, (
        "以下 Tool 子类既不在 tools/__init__.py 的 _TOOL_SPECS，也不在白名单：\n  "
        + "\n  ".join(missing)
        + "\n普通内置工具：在 _TOOL_SPECS 加一条 _ToolSpec(...)（按需选 "
        "gate/recorder/network 等注入标记）；构造依赖装配点注入（TaskManager / "
        "SkillLoader / 编排器引用）或属动态包装的：在 tests/"
        "test_tool_specs_completeness.py 的 WHITELIST 登记并写实理由。"
    )


def test_tool_specs_point_at_live_concrete_subclasses():
    """_TOOL_SPECS 的每个条目都必须是收集到的具体 Tool 子类。

    反向兜住清单烂掉：条目指向的类被改成抽象 / 不再继承 Tool（运行期
    default_tools() 构造会炸）时，这里先给出可读的定向报错。
    """
    collected = _collect_concrete_tool_subclasses()
    zombie = sorted(
        f"{spec.cls.__module__}.{spec.cls.__name__}"
        for spec in _TOOL_SPECS
        if spec.cls not in collected
    )
    assert not zombie, "_TOOL_SPECS 存在指向非具体 Tool 子类的僵尸条目：\n  " + "\n  ".join(zombie)


def test_whitelist_has_no_stale_entry():
    """白名单不许养陈旧条目：类已删除改名、或已登记进 _TOOL_SPECS 就要移出。

    与漏登记的测试两头夹住：白名单与「登记集合之外的具体工具类」恒等。
    """
    collected = _collect_concrete_tool_subclasses()
    declared = {spec.cls for spec in _TOOL_SPECS}
    stale = sorted(
        f"{cls.__module__}.{cls.__name__}"
        for cls in WHITELIST
        if cls in declared or cls not in collected
    )
    assert not stale, (
        "白名单陈旧条目（已登记进 _TOOL_SPECS 或类已不存在，请移除）：\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.parametrize(
    "sentinel", _COLLECTION_SENTINELS, ids=lambda cls: cls.__name__
)
def test_collection_reaches_every_definition_site(sentinel):
    """收集必须真到达每个定义点，否则完备性检查在残缺集合上假绿。"""
    assert sentinel in _collect_concrete_tool_subclasses(), (
        f"{sentinel.__module__}.{sentinel.__name__} 没被收集到：定义点导入断了，"
        "本文件的完备性检查不可信，先修 _collect_concrete_tool_subclasses 的导入清单"
    )
