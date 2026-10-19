"""Mod 工具 / UI action / dialog（P6）测试。"""

from __future__ import annotations

import json
import shutil

import pytest

from skysheep.core.mods_claude import ClaudeModManager
from skysheep.core.mods_node import find_node
from skysheep.core.mods_tool import ModTool
from skysheep.tools import ToolRegistry, default_tools
from skysheep.tools.base import Safety, ToolContext

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and find_node() is None,
    reason="需要 Node.js 18+",
)


def _make_mod(tmp_path, mod_id: str, main_js: str):
    mod_dir = tmp_path / "mods" / mod_id
    mod_dir.mkdir(parents=True)
    (mod_dir / "mod.json").write_text(json.dumps({
        "id": mod_id, "name": mod_id, "version": "1.0.0", "entry": "main.js",
    }), encoding="utf-8")
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")


@pytest.mark.asyncio
async def test_mod_tool_registered_and_invocable(tmp_path):
    """Mod 注册工具 → 元数据回到 Python → 调用时反向执行 Node handler。"""
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          await $.tool.register(
            { name: 'get_weather', description: '查天气', schema: {
                type: 'object', properties: { city: { type: 'string' } }, required: ['city'],
            } },
            async (args) => 'weather in ' + args.city + ': sunny',
          );
          return { decision: 'allow' };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        # 触发注册
        await mgr.on_tool_check("x", {}, "s")
        mod = mgr.mods["m"]
        assert any(t["name"] == "get_weather" for t in mod.registered_tools)

        # 构造 ModTool 并直接调用（模拟模型调用工具）
        spec = next(t for t in mod.registered_tools if t["name"] == "get_weather")
        tool = ModTool(
            mod_id="m", name="get_weather",
            description=spec["description"], schema=spec["schema"], loaded_mod=mod,
        )
        assert tool.safety == Safety.DANGEROUS  # 第三方工具一律需确认
        args = tool.args_model(city="Beijing")
        ctx = ToolContext(working_dir=tmp_path, session_id="s")
        result = await tool.run(args, ctx)
        assert result == "weather in Beijing: sunny"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mod_tool_schema_passthrough(tmp_path):
    """Mod 声明的原始 schema 原样给模型，不做 pydantic 反推。"""
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          await $.tool.register(
            { name: 'noop', description: 'd', schema: { type: 'object', properties: {
                nested: { type: 'object', properties: { a: { type: 'string' } } },
              } } },
            async () => 'ok',
          );
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        await mgr.on_tool_check("x", {}, "s")
        spec = mgr.mods["m"].registered_tools[0]
        tool = ModTool(mod_id="m", name="noop", description="d",
                       schema=spec["schema"], loaded_mod=mgr.mods["m"])
        schema = tool.to_schema()
        assert schema["input_schema"]["properties"]["nested"]["type"] == "object"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mod_tool_handler_error_raises(tmp_path):
    from skysheep.tools.base import ToolError

    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          await $.tool.register(
            { name: 'bad', description: 'd', schema: {} },
            async () => { throw new Error('handler boom'); },
          );
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        await mgr.on_tool_check("x", {}, "s")
        mod = mgr.mods["m"]
        tool = ModTool(mod_id="m", name="bad", description="d", schema={}, loaded_mod=mod)
        with pytest.raises(ToolError) as ei:
            await tool.run(tool.args_model(), ToolContext(working_dir=tmp_path))
        assert "handler boom" in str(ei.value)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_ui_action_dispatch(tmp_path):
    """ui.action 只投给发起它的 Mod，返回的元素树归一化。"""
    _make_mod(tmp_path, "m", """
        register('ui.action', ($, e) => {
          if (e.payload.actionId === 'refresh') {
            return {
              slot: 'pane',
              ui: { type: 'text', text: 'refreshed: ' + String(e.payload.value) },
            };
          }
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        entries = await mgr.on_action("m", "refresh", "click", "v1", "s")
        assert len(entries) == 1
        assert entries[0]["ui"]["slot"] == "pane"
        assert entries[0]["ui"]["tree"]["text"] == "refreshed: v1"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_ui_action_unknown_mod(tmp_path):
    _make_mod(tmp_path, "m", "register('ui.action', () => undefined);")
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        entries = await mgr.on_action("nope", "x", "click", None, "s")
        assert entries and entries[0].get("error")
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_dialog_slot_accepted(tmp_path):
    _make_mod(tmp_path, "m", """
        register('turn.stop', () => ({
          slot: 'dialog',
          ui: { type: 'box', direction: 'column', children: [
            { type: 'text', text: 'hello' },
            { type: 'button', id: 'ok', label: 'OK' },
          ] },
        }));
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        entries = await mgr.on_event("turn.stop", {}, "s")
        assert entries[0]["ui"]["slot"] == "dialog"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_agent_sees_mod_tool_in_registry(tmp_path):
    """Mod 注册的工具经 backend 风格注入后出现在注册表里（模拟 _build_full_registry）。"""
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          await $.tool.register(
            { name: 'echo_tool', description: '回显', schema: {} },
            async (args) => 'echo:' + JSON.stringify(args),
          );
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        await mgr.on_tool_check("x", {}, "s")
        # 模拟 backend._build_full_registry 的注入逻辑
        registry = ToolRegistry(default_tools())
        mod = mgr.mods["m"]
        for spec in mod.registered_tools:
            registry.register(ModTool(
                mod_id=mod.id, name=spec["name"],
                description=spec.get("description", ""),
                schema=spec.get("schema") or {}, loaded_mod=mod,
            ))
        assert registry.get("echo_tool") is not None
        # schema 出现在给模型的工具列表里
        names = [s["name"] for s in registry.schemas()]
        assert "echo_tool" in names
    finally:
        await mgr.close()
