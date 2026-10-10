"""扩展变更审计（audit.jsonl）与 MCP 溯源字段测试。"""

from __future__ import annotations

import json
from pathlib import Path

from skysheep.server.audit import AUDIT_READ_LIMIT, read_audit, record_audit


def test_record_and_read_roundtrip(tmp_path: Path):
    assert record_audit(tmp_path, kind="mcp", action="install", name="demo",
                        scope="global", detail="粘贴导入")
    assert record_audit(tmp_path, kind="skill", action="remove", name="old")
    items = read_audit(tmp_path)
    assert [i["action"] for i in items] == ["remove", "install"]  # 最近在前
    assert items[0]["name"] == "old"
    assert items[1]["detail"] == "粘贴导入"
    assert all("time" in i and "ts" in i for i in items)


def test_record_rejects_unknown_kind_action(tmp_path: Path):
    assert record_audit(tmp_path, kind="bogus", action="install", name="x") is None
    assert record_audit(tmp_path, kind="mcp", action="explode", name="x") is None
    assert read_audit(tmp_path) == []
    assert not (tmp_path / "audit.jsonl").exists()


def test_read_tolerates_missing_and_corrupt(tmp_path: Path):
    assert read_audit(tmp_path) == []  # 文件不存在
    (tmp_path / "audit.jsonl").write_text(
        json.dumps({"kind": "mcp", "action": "install", "name": "a"}) + "\n"
        + "not-json\n"
        + json.dumps({"kind": "evil-kind", "action": "install", "name": "b"}) + "\n",
        encoding="utf-8",
    )
    items = read_audit(tmp_path)
    assert len(items) == 1 and items[0]["name"] == "a"


def test_sanitize_and_limit(tmp_path: Path):
    long = "x" * 500
    record_audit(tmp_path, kind="mcp", action="install", name=long, detail=long)
    (item,) = read_audit(tmp_path)
    assert len(item["name"]) == 200 and len(item["detail"]) == 300
    for i in range(AUDIT_READ_LIMIT + 20):
        record_audit(tmp_path, kind="skill", action="enable", name=f"s{i}")
    items = read_audit(tmp_path, limit=AUDIT_READ_LIMIT)
    assert len(items) == AUDIT_READ_LIMIT
    assert items[0]["name"] == f"s{AUDIT_READ_LIMIT + 19}"  # 最近在前


def test_mcp_config_provenance_roundtrip(tmp_path: Path):
    """installed_from/installed_at 写入 mcp.json 后能原样读回；缺省兼容旧配置。"""
    from skysheep.mcp.client import MCPServerConfig
    from skysheep.mcp.installer import import_servers, load_servers, normalize_server

    cfg = normalize_server({
        "command": "echo", "args": ["hi"],
        "installed_from": "preset", "installed_at": "2026-10-10T12:00:00",
    })
    target = tmp_path / "mcp.json"
    import_servers({"demo": cfg}, target, overwrite=True, confirmed=True)
    loaded = load_servers(target)["demo"]
    assert loaded["installed_from"] == "preset"
    assert loaded["installed_at"] == "2026-10-10T12:00:00"
    # 旧配置（无溯源字段）不受影响
    legacy = normalize_server({"command": "echo"})
    assert legacy.installed_from == "" and legacy.installed_at == ""
    # pydantic 模型 dump 不含空字段以外噪音；手工 JSON 里的未知字段被忽略不报错
    assert MCPServerConfig.model_validate(
        json.loads('{"command": "echo", "future_field": 1}')
    ).command == "echo"
