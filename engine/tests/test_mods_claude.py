"""Claude Code 兼容模式（P2）测试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from skysheep.core.mods_claude import (
    DECISION_ALLOW,
    DECISION_ASK,
    DECISION_DENY,
    ClaudeModManager,
)
from skysheep.core.mods_node import find_node

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and find_node() is None,
    reason="需要 Node.js 18+",
)


def _make_mod(tmp_path: Path, mod_id: str, main_js: str, manifest_extra: dict | None = None) -> Path:
    mod_dir = tmp_path / "mods" / mod_id
    mod_dir.mkdir(parents=True)
    manifest = {
        "id": mod_id,
        "name": mod_id,
        "version": "1.0.0",
        "entry": "main.js",
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    (mod_dir / "mod.json").write_text(json.dumps(manifest), encoding="utf-8")
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")
    return mod_dir


@pytest.mark.asyncio
async def test_load_all(tmp_path):
    _make_mod(tmp_path, "mod-a", "register('tool.check', () => undefined);")
    _make_mod(tmp_path, "mod-b", "register('tool.check', () => undefined);")
    mgr = ClaudeModManager(enabled=True, enabled_ids=["mod-a", "mod-b"], root=tmp_path / "mods")
    mgr.load_all()
    assert set(mgr.mods.keys()) == {"mod-a", "mod-b"}
    await mgr.close()


@pytest.mark.asyncio
async def test_tool_check_allow(tmp_path):
    _make_mod(tmp_path, "mod-a", """
        register('tool.check', ($, e) => {
          if (e.payload.tool === 'run_command') return { decision: 'allow', reason: 'safe' };
          return undefined;
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["mod-a"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("run_command", {"cmd": "ls"}, "sess-1")
        assert r["decision"] == DECISION_ALLOW
        assert r["reason"] == "safe"
        r2 = await mgr.on_tool_check("other_tool", {}, "sess-1")
        assert r2["decision"] is None
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_tool_check_deny_short_circuits(tmp_path):
    _make_mod(tmp_path, "mod-a", """
        register('tool.check', ($, e) => {
          if (e.payload.tool === 'rm_rf') return { decision: 'deny', reason: 'never' };
          return undefined;
        });
    """)
    _make_mod(tmp_path, "mod-b", """
        register('tool.check', () => ({ decision: 'allow' }));
    """)
    mgr = ClaudeModManager(
        enabled=True, enabled_ids=["mod-a", "mod-b"], root=tmp_path / "mods",
    )
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("rm_rf", {}, "s")
        assert r["decision"] == DECISION_DENY
        assert r["reason"] == "never"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_tool_check_rewrite_input(tmp_path):
    _make_mod(tmp_path, "mod-a", """
        register('tool.check', ($, e) => {
          if (e.payload.tool === 'fs_write') {
            return { input: { ...e.payload.input, path: '/safe/' + e.payload.input.path } };
          }
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["mod-a"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("fs_write", {"path": "a.txt"}, "s")
        assert r["input"] == {"path": "/safe/a.txt"}
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_ask_priority(tmp_path):
    _make_mod(tmp_path, "mod-a", "register('tool.check', () => ({ decision: 'ask' }));")
    _make_mod(tmp_path, "mod-b", "register('tool.check', () => ({ decision: 'allow' }));")
    mgr = ClaudeModManager(
        enabled=True, enabled_ids=["mod-a", "mod-b"], root=tmp_path / "mods",
    )
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("any", {}, "s")
        assert r["decision"] == DECISION_ASK
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_disabled_returns_no_decision(tmp_path):
    _make_mod(tmp_path, "mod-a", "register('tool.check', () => ({ decision: 'deny' }));")
    mgr = ClaudeModManager(enabled=False, enabled_ids=["mod-a"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("any", {}, "s")
        assert r["decision"] is None
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_state_persists_across_calls(tmp_path):
    _make_mod(tmp_path, "mod-a", """
        register('tool.check', ($) => {
          const n = ($.state.get('n') || 0) + 1;
          $.state.set('n', n);
          return { decision: 'allow', reason: String(n) };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["mod-a"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r1 = await mgr.on_tool_check("x", {}, "s")
        r2 = await mgr.on_tool_check("x", {}, "s")
        assert r1["reason"] == "1"
        assert r2["reason"] == "2"
        state = json.loads((tmp_path / "mods" / "mod-a" / "state.json").read_text(encoding="utf-8"))
        assert state.get("n") == 2
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_handler_error_reported(tmp_path):
    _make_mod(tmp_path, "mod-a", "register('tool.check', () => { throw new Error('oops'); });")
    mgr = ClaudeModManager(enabled=True, enabled_ids=["mod-a"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("x", {}, "s")
        assert r["decision"] is None
        assert any("oops" in e for e in r["errors"])
    finally:
        await mgr.close()
