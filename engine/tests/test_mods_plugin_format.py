"""Claude Code 插件格式清单（P5）测试。"""

from __future__ import annotations

import json
import shutil

import pytest

from skysheep.core.mods_claude import (
    ClaudeModManager,
    _find_manifest,
    _normalize_manifest,
)
from skysheep.core.mods_node import find_node

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and find_node() is None,
    reason="需要 Node.js 18+",
)


def _write(tmp_path, rel: str, text: str):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_find_manifest_prefers_claude_plugin(tmp_path):
    _write(tmp_path, "mod.json", json.dumps({"id": "old", "entry": "main.js"}))
    _write(tmp_path, ".claude-plugin/plugin.json", json.dumps({"name": "new"}))
    path, data = _find_manifest(tmp_path)
    assert path is not None
    assert str(path).replace("\\", "/").endswith(".claude-plugin/plugin.json")
    assert data["name"] == "new"


def test_find_manifest_falls_back_to_mod_json(tmp_path):
    _write(tmp_path, "mod.json", json.dumps({"id": "old", "entry": "main.js"}))
    path, data = _find_manifest(tmp_path)
    assert path is not None and path.name == "mod.json"
    assert data["id"] == "old"


def test_normalize_plugin_json(tmp_path):
    _write(tmp_path, "hooks/main.js", "register('tool.check', () => undefined);")
    manifest = _normalize_manifest(tmp_path, {
        "name": "My Fancy Mod",
        "version": "1.2.3",
        "description": "demo",
        "hooks": ["tool.check"],
    })
    assert manifest["id"] == "my-fancy-mod"
    assert manifest["name"] == "My Fancy Mod"
    assert manifest["entry"] == "hooks/main.js"
    assert manifest["events"] == ["tool.check"]


def test_normalize_entry_falls_back_to_main_js(tmp_path):
    _write(tmp_path, "main.js", "register('x', () => undefined);")
    manifest = _normalize_manifest(tmp_path, {"name": "m"})
    assert manifest["entry"] == "main.js"


def test_normalize_defaults_to_all_events(tmp_path):
    manifest = _normalize_manifest(tmp_path, {"name": "m"})
    assert "tool.check" in manifest["events"]
    assert "tool.pre" in manifest["events"]


@pytest.mark.asyncio
async def test_load_cc_plugin_format(tmp_path):
    mod_dir = tmp_path / "mods" / "my-mod"
    (mod_dir / ".claude-plugin").mkdir(parents=True)
    (mod_dir / "hooks").mkdir()
    (mod_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "my-mod", "main": "hooks/main.js", "hooks": ["tool.check"],
    }), encoding="utf-8")
    (mod_dir / "hooks" / "main.js").write_text(
        "register('tool.check', () => ({ decision: 'allow', reason: 'cc' }));",
        encoding="utf-8",
    )
    mgr = ClaudeModManager(enabled=True, enabled_ids=["my-mod"], root=tmp_path / "mods")
    mgr.load_all()
    try:
        assert "my-mod" in mgr.mods
        r = await mgr.on_tool_check("x", {}, "s")
        assert r["decision"] == "allow"
        assert r["reason"] == "cc"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_gallery_cc_examples_load(tmp_path):
    """两个 CC 格式官方示例能被正确加载并注册事件。"""
    src = __import__("pathlib").Path(__file__).resolve().parents[2] / "mods-gallery"
    if not src.is_dir():
        pytest.skip("mods-gallery 不存在")
    mgr = ClaudeModManager(
        enabled=True, enabled_ids=["token-weather-cc", "blast-radius-cc"],
        root=src,
    )
    mgr.load_all()
    try:
        assert "token-weather-cc" in mgr.mods
        assert "blast-radius-cc" in mgr.mods
        assert mgr.mods["token-weather-cc"].entry == "hooks/main.js"
        # blast-radius-cc 的 tool.check 对高危命令返回 ask
        r = await mgr.on_tool_check(
            "run_command", {"command": "rm -rf /tmp/x"}, "s",
        )
        assert r["decision"] == "ask"
        # 无害命令不干预
        r2 = await mgr.on_tool_check("run_command", {"command": "ls"}, "s")
        assert r2["decision"] is None
    finally:
        await mgr.close()
