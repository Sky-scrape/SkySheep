"""代码卫生的机械守卫（11-07 全项目评审的系统性模式收口）。

评审暴露的典型问题不是「主路径写错」，而是「自声明纪律在边缘路径成批漏网」：
新工具忘写 MCP 四注解、状态文件 JSON 遇到非 UTF-8 直接让启动崩、有人绕过
textio 直接 json.dump。文字纪律挡不住后来者，这里把它们变成会变红的测试。

只放「能机械判定、不误报」的检查；语义正确性仍由各自的专项测试负责。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import skysheep.tools as tools_pkg
from skysheep.tools.base import Safety, Tool

SRC = Path(__file__).resolve().parents[1] / "src" / "skysheep"

ANNOTATION_KEYS = (
    "read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint",
)


def _all_tool_subclasses() -> list[type[Tool]]:
    """导入 tools 包的每个模块（含 core 里的工具），收集全部 Tool 子类。"""
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        try:
            importlib.import_module(f"skysheep.tools.{mod.name}")
        except Exception as exc:  # noqa: BLE001 - 可选依赖缺失的工具模块跳过
            pytest.skip(f"工具模块 {mod.name} 无法导入：{exc}")
    importlib.import_module("skysheep.core.subagent")
    importlib.import_module("skysheep.core.mods_tool")

    out: list[type[Tool]] = []
    stack = list(Tool.__subclasses__())
    while stack:
        cls = stack.pop()
        out.append(cls)
        stack.extend(cls.__subclasses__())
    return out


def test_every_tool_declares_safety_and_four_annotations():
    """每个内置工具自己写出 safety 与 MCP 四注解，不靠基类缺省兜底。

    基类缺省的 destructiveHint/openWorldHint 都是 true（规范保守值），
    新工具「忘写」不会报错但会把分级信息留成默认——外部工具目录与宿主
    拿到的是猜的，不是声明的。这里要求落在子类 __dict__ 里。
    """
    missing: list[str] = []
    for cls in _all_tool_subclasses():
        # 下划线开头的是内部基类（如团队总管工具的 _DirectorToolBase）：
        # 注解由它的具体子类声明，基类本身不直接注册进工具表
        if cls.__name__.startswith("_"):
            continue
        where = f"{cls.__module__}.{cls.__name__}"
        if "safety" not in cls.__dict__:
            missing.append(f"{where}: safety")
        for key in ANNOTATION_KEYS:
            if key not in cls.__dict__:
                missing.append(f"{where}: {key}")
    assert not missing, "工具缺少显式声明：\n" + "\n".join(sorted(missing))


def test_tool_safety_values_are_known_levels():
    bad = [
        f"{c.__module__}.{c.__name__}"
        for c in _all_tool_subclasses()
        if not isinstance(c.__dict__.get("safety", c.safety), Safety)
    ]
    assert not bad, f"safety 不是 Safety 枚举：{bad}"


def test_no_direct_json_dump_to_file():
    """状态文件写盘一律走 textio 原子写；json.dump 直接写文件会绕过它。"""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "json.dump(" in text:
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        "发现 json.dump( 直接写文件：引擎状态文件必须走 textio.write_text_atomic"
        f"（{offenders}）"
    )


def test_mcp_config_tolerates_non_utf8(tmp_path):
    """手编成 GBK 的 mcp.json 不能让启动崩：按空配置处理。"""
    from skysheep.mcp.installer import load_servers

    path = tmp_path / "mcp.json"
    path.write_bytes('{"mcpServers": {"坏": {"command": "x"}}}'.encode("gbk"))
    assert load_servers(path) == {}


def test_skill_state_tolerates_non_utf8(tmp_path):
    """skills-state.json 非 UTF-8 / 顶层不是对象时按「全部启用」处理。"""
    from skysheep.skills.loader import SkillLoader

    state = tmp_path / "skills-state.json"
    state.write_bytes('{"disabled": ["甲"]}'.encode("gbk"))
    loader = SkillLoader(global_dir=tmp_path / "skills", state_path=state)
    loader.discover()  # 不抛异常即通过（_apply_state 容忍坏状态文件）

    state.write_text('["not", "an", "object"]', encoding="utf-8")
    loader2 = SkillLoader(global_dir=tmp_path / "skills2", state_path=state)
    loader2.discover()
