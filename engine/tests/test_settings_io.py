"""设置导出/导入（zip 打包迁移）：导出含已存在的配置文件、导入恢复、非法包拒绝。"""

from __future__ import annotations

import base64
import io
import zipfile


def _zip_b64(files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_settings_export_import_roundtrip(home):
    """写入 config 与记忆 → 导出 zip → 清空 → 导入恢复 → 内容一致。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text("[providers.deepseek]\napi_key = 'k1'\n", encoding="utf-8")
    (home_dir / "memory.md").write_text("- 2026-01-01 喜欢深色主题\n", encoding="utf-8")

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "e1", "method": "settings.export", "params": {}})
        exp = recv_until(ws, "e1")["result"]
        assert "config.toml" in exp["included"]
        assert "memory.md" in exp["included"]
        assert exp["filename"].endswith(".zip")

        # 清空后导入
        (home_dir / "config.toml").unlink()
        (home_dir / "memory.md").unlink()
        ws.send_json({"id": "i1", "method": "settings.import",
                      "params": {"b64": exp["b64"]}})
        imp = recv_until(ws, "i1")["result"]
        assert set(imp["restored"]) >= {"config.toml", "memory.md"}
        assert (home_dir / "config.toml").read_text(encoding="utf-8").find("deepseek") >= 0
        assert "喜欢深色主题" in (home_dir / "memory.md").read_text(encoding="utf-8")


def test_settings_import_rejects_bad_zip(home):
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b1", "method": "settings.import", "params": {"b64": "!!!notzip"}})
        bad = recv_until(ws, "b1")
        assert not bad["ok"]

        # 合法 zip 但只含陌生文件 → 不恢复任何东西
        ws.send_json({"id": "b2", "method": "settings.import",
                      "params": {"b64": _zip_b64({"evil.exe": b"MZ..."})}})
        r2 = recv_until(ws, "b2")["result"]
        assert r2["restored"] == []


def test_settings_import_never_enables_accept_edits(home):
    """导入的 ui.json 不允许带着「自动允许写入」进来（等于绕过本机主动切换）。"""
    import json

    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    payload = _zip_b64({
        "ui.json": json.dumps({"accept_edits": 1, "sidebar_w": 300}).encode("utf-8"),
    })
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "settings.import", "params": {"b64": payload}})
        assert "ui.json" in recv_until(ws, "i1")["result"]["restored"]
        ws.send_json({"id": "m1", "method": "permission.mode"})
        assert recv_until(ws, "m1")["result"]["mode"] == "confirm"
    saved = json.loads((home_dir / "ui.json").read_text(encoding="utf-8"))
    assert "accept_edits" not in saved and saved.get("sidebar_w") == 300
