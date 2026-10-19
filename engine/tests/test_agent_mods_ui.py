"""Agent × Claude 兼容模式 UI 元素树（P3）集成测试。"""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.core.mods_claude import ClaudeModManager
from skysheep.core.mods_node import find_node
from skysheep.messages import TextBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools

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
    return mod_dir


def _make_agent(provider, tmp_path, mgr) -> Agent:
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=PermissionGate(working_dir=tmp_path),
        working_dir=tmp_path,
        max_iterations=3,
        mods_claude=mgr,
    )


@pytest.mark.asyncio
async def test_turn_stop_ui_tree_emitted(tmp_path):
    """turn.stop 返回 ui 元素树 → 产出 ModUI 事件且 payload.tree 已归一化。"""
    _make_mod(tmp_path, "m", """
        register('turn.stop', () => ({
          slot: 'pane',
          ui: {
            type: 'box', direction: 'column', children: [
              { type: 'text', text: '上下文：晴' },
              { type: 'button', id: 'refresh', label: '刷新' },
            ],
          },
        }));
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    provider = FakeProvider([[TextBlock(text="done")]])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        mod_uis = []
        async for ev in agent.run_turn("hi"):
            if ev.kind == "mod_ui":
                mod_uis.append(ev)
        assert mod_uis
        ui = mod_uis[0]
        assert ui.mod_id == "m"
        assert ui.slot == "pane"
        assert ui.payload["slot"] == "pane"
        tree = ui.payload["tree"]
        assert tree["type"] == "box"
        assert tree["children"][0]["text"] == "上下文：晴"
        assert tree["children"][1]["id"] == "refresh"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_invalid_ui_tree_dropped(tmp_path):
    """非法元素树整棵丢弃，产出 Notice 而非 ModUI。"""
    _make_mod(tmp_path, "m", """
        register('turn.stop', () => ({
          slot: 'pane',
          ui: { type: 'script', src: 'evil.js' },
        }));
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    provider = FakeProvider([[TextBlock(text="done")]])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        mod_uis, notices = [], []
        async for ev in agent.run_turn("hi"):
            if ev.kind == "mod_ui":
                mod_uis.append(ev)
            elif ev.kind == "notice":
                notices.append(ev)
        assert not mod_uis
        assert any("不合法" in (n.message or "") for n in notices)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_xss_text_not_escaped_but_textual(tmp_path):
    """文本里的 HTML 原样保留为字符串（前端走 textContent，不会执行）。"""
    _make_mod(tmp_path, "m", """
        register('turn.stop', () => ({
          slot: 'stream',
          ui: { type: 'text', text: '<img src=x onerror=alert(1)>' },
        }));
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    provider = FakeProvider([[TextBlock(text="done")]])
    agent = _make_agent(provider, tmp_path, mgr)
    try:
        mod_uis = []
        async for ev in agent.run_turn("hi"):
            if ev.kind == "mod_ui":
                mod_uis.append(ev)
        assert mod_uis
        # 数据层原样保留（前端 textContent 赋值才安全）
        assert "<img" in mod_uis[0].payload["tree"]["text"]
    finally:
        await mgr.close()
