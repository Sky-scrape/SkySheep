"""Agent × Claude Code 兼容模式（P2）集成测试。

验证 allow / ask / deny / rewrite 在真实 Agent 循环中的行为。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.core.mods_claude import ClaudeModManager
from skysheep.core.mods_node import find_node
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and find_node() is None,
    reason="需要 Node.js 18+",
)


def _make_mod(tmp_path: Path, mod_id: str, main_js: str) -> Path:
    mod_dir = tmp_path / "mods" / mod_id
    mod_dir.mkdir(parents=True)
    (mod_dir / "mod.json").write_text(json.dumps({
        "id": mod_id, "name": mod_id, "version": "1.0.0", "entry": "main.js",
    }), encoding="utf-8")
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")
    return mod_dir


def _make_agent(provider, tmp_path: Path, mgr: ClaudeModManager) -> Agent:
    gate = PermissionGate(working_dir=tmp_path)
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=gate,
        working_dir=tmp_path,
        max_iterations=6,
        mods_claude=mgr,
    )


async def _collect(agent: Agent, text: str, auto_respond: str = "allow_once"):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request" and auto_respond:
            agent.respond_permission(ev.request_id, auto_respond)
    return events


@pytest.mark.asyncio
async def test_mod_deny_short_circuits(tmp_path):
    """tool.check 给 deny：不进权限门，直接落 deny tool_result + 事件。"""
    _make_mod(tmp_path, "m", """
        register('tool.check', ($, e) => {
          if (e.payload.tool === 'run_command') return { decision: 'deny', reason: 'forbidden by mod' };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="run_command", input={"command": "echo hi"})],
        [TextBlock(text="done")],
    ])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        events = await _collect(agent, "go")
        assert not any(e.kind == "permission_request" for e in events)
        # 历史里有 deny 的 tool_result
        tool_results = [b for m in agent.history for b in m.content
                        if getattr(b, "type", "") == "tool_result"]
        assert any("forbidden by mod" in (r.content or "") for r in tool_results)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mod_ask_forces_permission_even_readonly(tmp_path):
    """tool.check 给 ask：即使 READONLY 也强制弹权限卡。"""
    _make_mod(tmp_path, "m", """
        register('tool.check', ($, e) => {
          if (e.payload.tool === 'read_file') return { decision: 'ask', reason: 'see this?' };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    target = tmp_path / "a.txt"
    target.write_text("hi", encoding="utf-8")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": str(target)})],
        [TextBlock(text="ok")],
    ])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        events = await _collect(agent, "go", auto_respond="allow_once")
        assert any(e.kind == "permission_request" for e in events)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mod_allow_skips_permission(tmp_path):
    """tool.check 给 allow：即使本该弹窗的写入也不弹（引擎主目录除外）。"""
    _make_mod(tmp_path, "m", """
        register('tool.check', ($, e) => {
          if (e.payload.tool === 'write_file') return { decision: 'allow' };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    target = tmp_path / "out.txt"
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file", input={"path": str(target), "content": "hello"})],
        [TextBlock(text="done")],
    ])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        events = await _collect(agent, "go", auto_respond=None)
        # 无权限请求（mod 已放行）
        assert not any(e.kind == "permission_request" for e in events)
        assert target.read_text(encoding="utf-8") == "hello"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mod_rewrite_input_takes_effect(tmp_path):
    """tool.check 返回 input 字段：改写工具参数并同步到历史。"""
    target = tmp_path / "orig.txt"
    target.write_text("original", encoding="utf-8")
    safe_target = tmp_path / "safe.txt"
    _make_mod(tmp_path, "m", f"""
        register('tool.check', ($, e) => {{
          if (e.payload.tool === 'write_file') {{
            return {{ input: {{ ...e.payload.input, path: {json.dumps(str(safe_target))} }} }};
          }}
        }});
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file",
                      input={"path": str(target), "content": "rewritten"})],
        [TextBlock(text="done")],
    ])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        await _collect(agent, "go")
        # 目标文件原值未变；安全路径被写入
        assert target.read_text(encoding="utf-8") == "original"
        assert safe_target.read_text(encoding="utf-8") == "rewritten"
        # 历史里的 assistant tool_use.input 也是改写后的值
        asst_msgs = [m for m in agent.history if m.role == "assistant"]
        t1_blocks = [b for m in asst_msgs for b in m.content
                     if getattr(b, "type", "") == "tool_use" and b.id == "t1"]
        assert t1_blocks
        assert t1_blocks[0].input["path"] == str(safe_target)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mod_disabled_does_not_interfere(tmp_path):
    """mods 关闭时不干预正常流程（读文件照常走 READONLY 免确认）。"""
    _make_mod(tmp_path, "m", "register('tool.check', () => ({ decision: 'deny' }));")
    mgr = ClaudeModManager(enabled=False, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    target = tmp_path / "a.txt"
    target.write_text("hi", encoding="utf-8")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": str(target)})],
        [TextBlock(text="ok")],
    ])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        await _collect(agent, "go", auto_respond=None)
        # 未被 deny：tool_result 是成功内容
        tool_results = [b for m in agent.history for b in m.content
                        if getattr(b, "type", "") == "tool_result"]
        assert tool_results and "hi" in tool_results[0].content
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mod_ask_disables_always_rule(tmp_path):
    """ask 路径的权限卡没有「总是允许」——防止一次点选永久绕过 Mod。"""
    _make_mod(tmp_path, "m", """
        register('tool.check', ($, e) => {
          if (e.payload.tool === 'write_file') return { decision: 'ask' };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    target = tmp_path / "out.txt"
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file", input={"path": str(target), "content": "hi"})],
        [TextBlock(text="done")],
    ])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        events = await _collect(agent, "go", auto_respond="allow_once")
        perm_events = [e for e in events if e.kind == "permission_request"]
        assert perm_events
        # rule_kind 应为空（没有 always 规则）
        assert not perm_events[0].rule_kind
    finally:
        await mgr.close()
