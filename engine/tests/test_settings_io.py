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


def test_settings_import_goes_through_atomic_entries(home, monkeypatch):
    """设置导入的落盘必须全部走 textio 原子入口（write_bytes_atomic /
    write_text_atomic），backend.py 内绑定的入口被记录调用；卫兵把
    「直接 Path.write_bytes / Path.write_text 写这三个目标」当场炸出，
    防止将来回退成非原子写。"""
    import json
    from pathlib import Path

    from test_server import make_client, recv_until

    import skysheep.server.backend as backend_mod
    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text("[providers.old]\napi_key = 'old'\n", encoding="utf-8")

    real_ba = backend_mod.write_bytes_atomic
    real_ta = backend_mod.write_text_atomic
    via_atomic: list[tuple[str, str]] = []  # (入口种类, 文件名)

    def rec_ba(path, data, **kw):
        via_atomic.append(("bytes", path.name))
        return real_ba(path, data, **kw)

    def rec_ta(path, text, *args, **kw):
        via_atomic.append(("text", path.name))
        return real_ta(path, text, *args, **kw)

    monkeypatch.setattr(backend_mod, "write_bytes_atomic", rec_ba)
    monkeypatch.setattr(backend_mod, "write_text_atomic", rec_ta)

    # 卫兵：导入目标若被绕开原子入口直接 Path 写，立刻失败（只盯这三个名字，
    # 不影响启动期其他文件的正常写法）
    guarded = {"config.toml", "memory.md", "ui.json"}
    real_pb, real_pt = Path.write_bytes, Path.write_text

    def deny_pb(self, data, **kw):
        if self.name in guarded and self.parent == home_dir:
            raise AssertionError(f"{self.name} 被非原子入口直接写盘（Path.write_bytes）")
        return real_pb(self, data, **kw)

    def deny_pt(self, text, **kw):
        if self.name in guarded and self.parent == home_dir:
            raise AssertionError(f"{self.name} 被非原子入口直接写盘（Path.write_text）")
        return real_pt(self, text, **kw)

    monkeypatch.setattr(Path, "write_bytes", deny_pb)
    monkeypatch.setattr(Path, "write_text", deny_pt)

    payload = _zip_b64({
        "config.toml": b"[providers.new]\napi_key = 'new'\n",
        "memory.md": "- 新记忆\n".encode(),
        "ui.json": json.dumps({"theme": "纸墨"}).encode("utf-8"),
    })
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "ia1", "method": "settings.import", "params": {"b64": payload}})
        r = recv_until(ws, "ia1")
        assert r["ok"], r.get("error")
        assert set(r["result"]["restored"]) == {"config.toml", "memory.md", "ui.json"}

    assert ("bytes", "config.toml") in via_atomic
    assert ("bytes", "memory.md") in via_atomic
    assert ("text", "ui.json") in via_atomic, "ui.json 走 _strip_accept_edits 也要原子写"
    assert "providers.new" in (home_dir / "config.toml").read_text(encoding="utf-8")
    assert [p.name for p in home_dir.glob("*.tmp")] == []


def test_settings_import_atomic_replace_failure_rolls_back_clean(home, monkeypatch):
    """原子替换那一步失败（os.replace 抛错）：回滚后原文件字节原样保留、
    导入前不存在的文件不残留、数据目录不留临时文件。

    注入点取真正的替换原语 os.replace：只对包里的 memory.md 抛
    PermissionError，其余放行。config.toml 导入前已有内容 → 回滚必须
    按导入前快照还原成原字节（全有或全无）。
    """
    import json
    import os

    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text("[providers.old]\napi_key = 'old'\n", encoding="utf-8")

    target_md = home_dir / "memory.md"
    real_replace = os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        if str(dst) == str(target_md):
            raise PermissionError(32, "another process holds the file (probe)")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)

    payload = _zip_b64({
        "config.toml": b"[providers.new]\napi_key = 'new'\n",
        "memory.md": "- 新记忆\n".encode(),
        "ui.json": json.dumps({"theme": "纸墨"}).encode("utf-8"),
    })
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # boot 时 load_config 会把明文 Key 升级为 DPAPI 密文并回写、ui.json 也会在
        # boot 期落偏好——导入前的磁盘字节要在连接建立后取，回滚断言比对的就是
        # 这份「当时」的样子（memory.md 导入前确实不存在，不受影响）
        pre_cfg = (home_dir / "config.toml").read_bytes()
        pre_ui = (home_dir / "ui.json").read_bytes() if (home_dir / "ui.json").is_file() else None
        ws.send_json({"id": "ir1", "method": "settings.import", "params": {"b64": payload}})
        bad = recv_until(ws, "ir1")
        assert not bad["ok"] and "回滚" in bad["error"]

    assert (home_dir / "config.toml").read_bytes() == pre_cfg, "回滚必须还原导入前字节"
    assert not target_md.exists(), "导入前不存在：新写的要被回滚删掉"
    post_ui = (home_dir / "ui.json").read_bytes() if (home_dir / "ui.json").is_file() else None
    assert post_ui == pre_ui, "ui.json 必须回到导入前状态（包里的内容不得残留）"
    assert [p.name for p in home_dir.glob("*.tmp")] == [], "原子写失败要自清临时文件"


def test_settings_preview_import_lists_and_flags(home):
    """预览：列出白名单文件、跳过清单外文件、标记敏感内容；不落盘。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    payload = _zip_b64({
        "config.toml": b"[providers.x]\napi_key = 'secret'\n",
        "memory.md": b"# memory\n",
        "evil.exe": b"MZ...",
    })

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "settings.preview_import",
                      "params": {"b64": payload}})
        pv = recv_until(ws, "p1")["result"]
        names = {it["name"] for it in pv["items"]}
        assert names == {"config.toml", "memory.md"}
        assert pv["unknown"] == ["evil.exe"]
        assert pv["contains_secrets"] is True
        assert next(it for it in pv["items"] if it["name"] == "config.toml")["has_secrets"] is True
        # 预览不落盘
        assert not (home_dir / "config.toml").exists()
        assert not (home_dir / "memory.md").exists()

        # 坏 zip 拒绝
        ws.send_json({"id": "p2", "method": "settings.preview_import",
                      "params": {"b64": "!!!bad"}})
        frame = recv_until(ws, "p2")
        assert not frame["ok"] and "zip 解析失败" in frame["error"]


def test_export_full_data_zip(home):
    """完整导出：含数据库快照与技能文件；默认打码 config，include_secrets 原样。"""
    import zipfile

    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text(
        "[providers.deepseek]\napi_key = 'sk-secret-value'\n", encoding="utf-8"
    )
    sk = home_dir / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\n", encoding="utf-8")

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "f1", "method": "data.export_full", "params": {}})
        r = recv_until(ws, "f1")["result"]
        assert any("skysheep.db" in str(i) for i in r["included"])
        assert r["include_secrets"] is False
        z = zipfile.ZipFile(r["path"])
        cfg = z.read("config.toml").decode("utf-8")
        assert "sk-secret-value" not in cfg and "***" in cfg  # 打码
        assert "skills/demo/SKILL.md" in z.namelist()
        assert "README.txt" in z.namelist()

        ws.send_json({"id": "f2", "method": "data.export_full",
                      "params": {"include_secrets": True}})
        r2 = recv_until(ws, "f2")["result"]
        cfg2 = zipfile.ZipFile(r2["path"]).read("config.toml").decode("utf-8")
        assert "sk-secret-value" in cfg2
