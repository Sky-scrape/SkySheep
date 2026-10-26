"""审计 M14：load_mcp_configs 对 mcpServers 段非 JSON 对象的容错。

报告 M14：client.load_mcp_configs 原来写的是 ``data.get("mcpServers") or {}``，
但 or 只兜得住空值——mcpServers 是非空 list/字符串/数字时是真值，``.items()``
直接 AttributeError。四个调用点（backend setup、backend_parts/mcp、cli/app
两处）都是裸调，畸形 mcp.json 会让桌面端落到启动失败页、CLI 直接崩。
修复口径与 installer.load_servers 一致：不是 dict 就记可读告警、按空配置继续，
绝不抛异常；null/空数组/缺键等既有静默降级保持不变。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skysheep.mcp.client import load_mcp_configs


@pytest.fixture(autouse=True)
def _isolated_home(home):
    """本文件全部用例跑在进程级隔离的 SKYSHEEP_HOME 下（系统临时目录），
    绝不碰真实 ~/.skysheep。"""
    return home


def _write_raw(path: Path, raw: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")


def _valid_servers() -> dict:
    return {
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
        }
    }


# 畸形但合法的 JSON：非空值都是真值，修复前 or 兜不住、.items() 直接炸。
# list / str / int 至少各一，再补 bool（int 子类）与 float。
MALFORMED_SERVERS = [
    ("非空数组", '[{"command": "uvx"}]'),
    ("非空字符串", '"just a string"'),
    ("非零整数", "42"),
    ("布尔真值", "true"),
    ("浮点数", "3.14"),
]


@pytest.mark.parametrize(
    ("label", "raw"), MALFORMED_SERVERS, ids=[m[0] for m in MALFORMED_SERVERS]
)
def test_malformed_mcp_servers_warns_and_degrades(tmp_path, label, raw):
    """mcpServers 段是畸形 JSON 对象：记可读告警、按空配置继续，绝不抛异常。"""
    p = tmp_path / "mcp.json"
    _write_raw(p, json.dumps({"mcpServers": json.loads(raw)}, ensure_ascii=False))
    configs, warnings = load_mcp_configs(p, None)
    assert configs == {}
    assert len(warnings) == 1
    assert "mcpServers" in warnings[0]
    assert "不是 JSON 对象" in warnings[0]
    assert "mcp.json" in warnings[0]  # 告警点名是哪个文件


def test_malformed_mcp_servers_in_project_file(tmp_path):
    """项目级配置同样容错，告警带「项目」标签。"""
    p = tmp_path / "proj-mcp.json"
    _write_raw(p, json.dumps({"mcpServers": ["oops"]}))
    configs, warnings = load_mcp_configs(None, p)
    assert configs == {}
    assert len(warnings) == 1
    assert "项目" in warnings[0]
    assert "mcpServers" in warnings[0]


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("空数组", "[]"),  # 既有静默降级：or 兜底成 {}，不告警
        ("null", "null"),
        ("空字符串", '""'),
        ("数字零", "0"),
    ],
    ids=["空数组", "null", "空字符串", "数字零"],
)
def test_falsy_mcp_servers_stay_silent(tmp_path, label, raw):
    """null/空数组等空值保持既有降级：静默按空配置处理，不加新告警。"""
    p = tmp_path / "mcp.json"
    _write_raw(p, json.dumps({"mcpServers": json.loads(raw)}))
    configs, warnings = load_mcp_configs(p, None)
    assert configs == {}
    assert warnings == []


def test_missing_mcp_servers_key_stay_silent(tmp_path):
    """缺 mcpServers 键：既有行为是静默空配置，不回归。"""
    p = tmp_path / "mcp.json"
    _write_raw(p, json.dumps({"other": {"a": 1}}))
    configs, warnings = load_mcp_configs(p, None)
    assert configs == {}
    assert warnings == []


def test_valid_mcp_servers_unchanged(tmp_path):
    """合法对象路径零变化：服务照常加载、无告警。"""
    p = tmp_path / "mcp.json"
    _write_raw(p, json.dumps(_valid_servers(), ensure_ascii=False))
    configs, warnings = load_mcp_configs(p, None)
    assert set(configs) == {"fetch"}
    assert configs["fetch"].transport == "stdio"
    assert warnings == []


def test_empty_mcp_servers_object_unchanged(tmp_path):
    """mcpServers 为空对象：合法路径，空配置、无告警。"""
    p = tmp_path / "mcp.json"
    _write_raw(p, json.dumps({"mcpServers": {}}))
    configs, warnings = load_mcp_configs(p, None)
    assert configs == {}
    assert warnings == []


def test_malformed_global_does_not_block_valid_project(tmp_path):
    """全局配置畸形只影响自己：项目级合法服务照常合并进来。"""
    gp = tmp_path / "global.json"
    pp = tmp_path / "proj.json"
    _write_raw(gp, json.dumps({"mcpServers": "not a dict"}))
    _write_raw(pp, json.dumps(_valid_servers()))
    configs, warnings = load_mcp_configs(gp, pp)
    assert set(configs) == {"fetch"}
    assert len(warnings) == 1
    assert "全局" in warnings[0]
