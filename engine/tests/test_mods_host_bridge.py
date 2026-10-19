"""Host 桥（P4）：Node 反向调用 Python 引擎能力。"""

from __future__ import annotations

import json
import shutil

import pytest

from skysheep.core.mods_claude import ClaudeModManager
from skysheep.core.mods_node import find_node
from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and find_node() is None,
    reason="需要 Node.js 18+",
)


def _make_mod(tmp_path, mod_id, main_js):
    mod_dir = tmp_path / "mods" / mod_id
    mod_dir.mkdir(parents=True)
    (mod_dir / "mod.json").write_text(json.dumps({
        "id": mod_id, "name": mod_id, "version": "1.0.0", "entry": "main.js",
    }), encoding="utf-8")
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")


@pytest.mark.asyncio
async def test_store_roundtrip(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          const n = (await $.store.get('n') || 0) + 1;
          await $.store.set('n', n);
          return { decision: 'allow', reason: String(n) };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r1 = await mgr.on_tool_check("x", {}, "s")
        r2 = await mgr.on_tool_check("x", {}, "s")
        assert r1["reason"] == "1"
        assert r2["reason"] == "2"
        store = json.loads((tmp_path / "mods" / "m" / "store.json").read_text(encoding="utf-8"))
        assert store["n"] == 2
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_store_delete(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          await $.store.set('k', 'v');
          const before = await $.store.get('k');
          await $.store.delete('k');
          const after = await $.store.get('k');
          return { reason: JSON.stringify({ before, after }) };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("x", {}, "s")
        assert json.loads(r["reason"]) == {"before": "v", "after": None}
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_settings_get(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          const v = await $.settings.get('theme');
          return { reason: String(v) };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.settings_getter = lambda: {"theme": "dark"}
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("x", {}, "s")
        assert r["reason"] == "dark"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_model_complete(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          const out = await $.model.complete({ prompt: 'say hi' });
          return { reason: out.text };
        });
    """)
    provider = FakeProvider([[TextBlock(text="hello from model")]])
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.provider_getter = lambda: provider
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("x", {}, "s")
        assert r["reason"] == "hello from model"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_model_complete_no_provider(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          try {
            await $.model.complete({ prompt: 'hi' });
            return { reason: 'no error' };
          } catch (err) {
            return { reason: 'caught: ' + err.message };
          }
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("x", {}, "s")
        assert "caught" in r["reason"] and "provider" in r["reason"]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_tool_register_recorded(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          await $.tool.register({
            name: 'my_tool',
            description: 'does things',
            schema: { type: 'object', properties: {} },
          }, async () => 'ok');
          return { decision: 'allow' };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        await mgr.on_tool_check("x", {}, "s")
        mod = mgr.mods["m"]
        assert any(t["name"] == "my_tool" for t in mod.registered_tools)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_mcp_list_without_manager(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          const servers = await $.mcp.list();
          return { reason: JSON.stringify(servers) };
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("x", {}, "s")
        assert json.loads(r["reason"]) == []
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_unknown_host_method_errors(tmp_path):
    _make_mod(tmp_path, "m", """
        register('tool.check', async ($) => {
          try {
            await globalThis.__sky_host_call__('nope.method', {});
            return { reason: 'no error' };
          } catch (err) {
            return { reason: 'caught: ' + err.message };
          }
        });
    """)
    mgr = ClaudeModManager(enabled=True, enabled_ids=["m"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        r = await mgr.on_tool_check("x", {}, "s")
        assert "未知 host 方法" in r["reason"]
    finally:
        await mgr.close()
