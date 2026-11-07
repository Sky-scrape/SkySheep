"""Claude Code 插件格式安装（P5）测试。"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

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


def _make_cc_plugin_src(src: Path, name: str) -> Path:
    """造一个最小 CC 插件格式来源目录（.claude-plugin/plugin.json + hooks/main.js）。"""
    (src / ".claude-plugin").mkdir(parents=True)
    (src / "hooks").mkdir()
    (src / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": name, "version": "1.0.0",
        "main": "hooks/main.js", "hooks": ["tool.check"],
    }), encoding="utf-8")
    (src / "hooks" / "main.js").write_text(
        "register('tool.check', () => ({ decision: 'allow' }));", encoding="utf-8",
    )
    return src


@pytest.mark.asyncio
async def test_confirm_install_routes_to_claude_table(home):
    """Claude 兼容模式下本地安装：confirm 后名单进 [mods_claude]，[mods] 不被误开。

    回归：confirm_install_mod 曾固定读 [mods] 写 [mods]，不按 _mods_mode 路由
    （同模块 install_official_mod / delete_mod / toggle_mod / set_mods_enabled
    都走 _set_mods_cfg）。CC 格式本地安装（.claude-plugin 是开放路径）后接口
    返回 enabled:True 实际未激活（假启用），且 [mods].enabled 被误置 True，
    切回 quickjs 模式时全部 Mod 被静默启用。
    """
    from skysheep.config import _read_raw_config, set_mods_claude_in_config
    from skysheep.server.backend import ServerBackend

    set_mods_claude_in_config(enabled=True, enabled_mods=[])
    src = _make_cc_plugin_src(home / "claude-plugin-src", "claude-routed")

    backend = ServerBackend(working_dir=home / "proj", provider_factory=lambda: None)
    # provider / mcp 在 setup() 才装配；claude 管理器构建只存引用，占位即可
    backend.provider = None
    backend.mcp = None
    preview = await backend.install_mod(str(src))
    result = await backend.confirm_install_mod(preview["install_token"])
    assert result == {"installed": "claude-routed", "enabled": True}

    _p, raw = _read_raw_config()
    assert "claude-routed" in raw["mods_claude"]["enabled_mods"], raw
    assert raw["mods_claude"]["enabled"] is True
    # 误开回归：这条安装不得碰 [mods] 表（否则切回 quickjs 全部静默启用）
    assert "mods" not in raw, raw.get("mods")
    # 热重载走 claude 管理器：新 Mod 真挂上了，quickjs 侧保持为空
    assert backend.mods is None
    assert backend.mods_claude is not None
    assert "claude-routed" in backend.mods_claude.mods


@pytest.mark.asyncio
async def test_confirm_install_quickjs_table_unchanged(home):
    """quickjs 模式（默认）不回归：confirm 后名单照旧进 [mods]，不新建 [mods_claude]。"""
    from skysheep.config import _read_raw_config
    from skysheep.server.backend import ServerBackend

    src = home / "quickjs-src"
    src.mkdir()
    (src / "mod.json").write_text(json.dumps({
        "id": "quickjs-routed", "name": "quickjs-routed", "version": "1.0.0",
        "hooks": ["tool_pre"], "permissions": "observe",
    }), encoding="utf-8")
    (src / "main.js").write_text("export default {};", encoding="utf-8")

    backend = ServerBackend(working_dir=home / "proj", provider_factory=lambda: None)
    preview = await backend.install_mod(str(src))
    result = await backend.confirm_install_mod(preview["install_token"])
    assert result == {"installed": "quickjs-routed", "enabled": True}

    _p, raw = _read_raw_config()
    assert "quickjs-routed" in raw["mods"]["enabled_mods"], raw
    assert raw["mods"]["enabled"] is True
    assert "mods_claude" not in raw, raw.get("mods_claude")


@pytest.mark.asyncio
async def test_install_rejects_manifestless(home, tmp_path):
    from skysheep.server.backend import ServerBackend

    src = tmp_path / "empty-dir"
    src.mkdir()
    (src / "main.js").write_text("register('x', () => undefined);", encoding="utf-8")
    backend = ServerBackend(working_dir=home / "proj", provider_factory=lambda: None)
    with pytest.raises(ModError):
        await backend.install_mod(str(src))
