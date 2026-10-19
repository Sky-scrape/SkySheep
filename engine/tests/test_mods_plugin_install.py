"""Claude Code 插件格式安装（P5）测试。"""

from __future__ import annotations

import json
import shutil
import zipfile

import pytest

from skysheep.core.mods import (
    ModError,
    convert_claude_plugin_manifest,
    is_claude_plugin_manifest,
    parse_manifest,
)
from skysheep.core.mods_node import find_node

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and find_node() is None,
    reason="需要 Node.js 18+",
)


def test_is_claude_plugin_by_path(tmp_path):
    p = tmp_path / ".claude-plugin" / "plugin.json"
    p.parent.mkdir(parents=True)
    p.write_text("{}", encoding="utf-8")
    assert is_claude_plugin_manifest({}, p)


def test_is_claude_plugin_by_dotted_hooks():
    assert is_claude_plugin_manifest({"hooks": ["tool.check", "turn.stop"]})
    assert not is_claude_plugin_manifest({"id": "x", "hooks": ["tool_pre"]})


def test_convert_minimal():
    out = convert_claude_plugin_manifest({"name": "My Mod"})
    assert out["id"] == "my-mod"
    assert out["entry"] == "main.js"
    assert out["permissions"] == "tighten"
    assert out["cc_mods"]["compatible"] is True


def test_convert_maps_dotted_hooks():
    out = convert_claude_plugin_manifest({
        "name": "x", "hooks": ["tool.check", "tool.post", "turn.stop", "unknown.event"],
    })
    assert out["hooks"] == ["tool_pre", "tool_post", "turn_stop"]


def test_convert_wider_permissions_tightened():
    out = convert_claude_plugin_manifest({"name": "x", "permissions": "full"})
    assert out["permissions"] == "tighten"


def test_convert_parses_after_conversion():
    """转换后必须能通过旧 parse_manifest 的严格校验。"""
    data = convert_claude_plugin_manifest({
        "name": "Demo Plugin",
        "version": "2.0.0",
        "main": "hooks/main.js",
        "hooks": ["tool.check"],
        "permissions": "full",
        "homepage": "https://example.com",
    })
    manifest = parse_manifest(data, source_label="test")
    assert manifest["id"] == "demo-plugin"
    assert manifest["entry"] == "hooks/main.js"
    assert manifest["hooks"] == ["tool_pre"]
    assert manifest["permissions"] == "tighten"


@pytest.mark.asyncio
async def test_install_cc_plugin_folder(home):
    """从文件夹安装 CC 插件格式（.claude-plugin/plugin.json）。"""
    from skysheep.server.backend import ServerBackend

    src = home / "plugin-src"
    (src / ".claude-plugin").mkdir(parents=True)
    (src / "hooks").mkdir()
    (src / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "demo-plugin", "version": "1.0.0",
        "main": "hooks/main.js", "hooks": ["tool.check"],
    }), encoding="utf-8")
    (src / "hooks" / "main.js").write_text(
        "register('tool.check', () => ({ decision: 'allow' }));", encoding="utf-8",
    )

    backend = ServerBackend(working_dir=home / "proj", provider_factory=lambda: None)
    preview = await backend.install_mod(str(src))
    assert preview["needs_confirm"] is True
    assert preview["id"] == "demo-plugin"
    assert preview["hooks"] == ["tool_pre"]
    assert preview["entry"] == "hooks/main.js"

    result = await backend.confirm_install_mod(preview["install_token"])
    assert result["installed"] == "demo-plugin"
    # 落盘后目录里应同时保留 .claude-plugin/plugin.json 与 hooks/main.js
    from skysheep.config import skysheep_home

    target = skysheep_home() / "mods" / "demo-plugin"
    assert (target / ".claude-plugin" / "plugin.json").is_file()
    assert (target / "hooks" / "main.js").is_file()


@pytest.mark.asyncio
async def test_install_cc_plugin_zip(home, tmp_path):
    """从 .zip 安装 CC 插件格式（带外层包装目录）。"""
    from skysheep.server.backend import ServerBackend

    zip_path = tmp_path / "plugin.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("wrapper/.claude-plugin/plugin.json", json.dumps({
            "name": "zipped-plugin", "main": "hooks/main.js", "hooks": ["tool.check"],
        }))
        zf.writestr(
            "wrapper/hooks/main.js",
            "register('tool.check', () => ({ decision: 'allow' }));",
        )

    backend = ServerBackend(working_dir=home / "proj", provider_factory=lambda: None)
    preview = await backend.install_mod(str(zip_path))
    assert preview["id"] == "zipped-plugin"
    result = await backend.confirm_install_mod(preview["install_token"])
    assert result["installed"] == "zipped-plugin"


@pytest.mark.asyncio
async def test_install_rejects_manifestless(home, tmp_path):
    from skysheep.server.backend import ServerBackend

    src = tmp_path / "empty-dir"
    src.mkdir()
    (src / "main.js").write_text("register('x', () => undefined);", encoding="utf-8")
    backend = ServerBackend(working_dir=home / "proj", provider_factory=lambda: None)
    with pytest.raises(ModError):
        await backend.install_mod(str(src))
