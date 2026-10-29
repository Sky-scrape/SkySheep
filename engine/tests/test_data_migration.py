"""数据迁移与导出/导入的恢复链路：完整导出 → 全新环境导入 → 内容可用。

「导出成功、导入后不能继续工作」是迁移功能的头号事故形态，这里把真正的
跨机器恢复做成回归：A 环境导出 → 全新 SKYSHEEP_HOME 导入 → 会话、配置、
技能逐项核对。另覆盖导入的失败面：坏库 / 新版库 / 坏配置在预览或写入前
被拦下、写入失败自动回滚（全有或全无）。
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile

import pytest

from skysheep import config as config_mod
from skysheep.messages import TextBlock
from skysheep.session.store import SCHEMA_VERSION


@pytest.fixture(autouse=True)
def _freeze_credential_migration(monkeypatch):
    """把 config 的明文凭据 DPAPI 迁移钉在「已迁移过」（该标志是进程级 run-once）。

    进程内第一个读到明文凭据的用例会触发整份配置加密回写，之后的用例都按
    已迁移处理。串行下谁是「第一个」固定不变；xdist 下随 worker 分派漂移，
    同一用例这次触发迁移（config.toml 变 dpapi: 密文）、下次没触发，本文件
    里断言本机 config.toml 保持明文的用例就随调度红绿不定（-n 4 实测：
    test_import_full_rejects_db_from_newer_version 的 "keep" in config.toml
    读到 dpapi 密文）。统一按已迁移处理后本文件全程不触发迁移写盘，明文
    断言恢复确定性；迁移行为本身由 test_secure_store.py 覆盖（那边有逐用例
    复位的夹具）。
    """
    monkeypatch.setattr(config_mod, "_migration_attempted", True)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write_zip(path, files: dict[str, bytes]) -> None:
    path.write_bytes(_zip_bytes(files))


def test_full_export_import_roundtrip_cross_home(home, monkeypatch):
    """A 环境导出 → 全新 B 环境导入：会话、配置、记忆、技能全部可用。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_a = skysheep_home()
    home_a.mkdir(parents=True, exist_ok=True)
    (home_a / "config.toml").write_text(
        "[providers.deepseek]\napi_key = 'sk-migration-key'\n", encoding="utf-8"
    )
    (home_a / "memory.md").write_text("- 2026-10-11 迁移前记忆\n", encoding="utf-8")
    sk = home_a / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: 迁移测试技能\n---\n", encoding="utf-8")

    script = [[TextBlock(text="迁移回答")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "跨机迁移测试消息"}})
        frame = recv_until(ws, "c1")
        assert frame["ok"]
        ws.send_json({"id": "e1", "method": "data.export_full",
                      "params": {"include_secrets": True}})
        exp = recv_until(ws, "e1")["result"]
        assert any("skysheep.db" in i for i in exp["included"])

    # —— 全新 B 环境（新 SKYSHEEP_HOME，等价另一台电脑的首次启动）——
    home_b = home / "homeB"
    monkeypatch.setenv("SKYSHEEP_HOME", str(home_b))
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "data.preview_import_full",
                      "params": {"path": exp["path"]}})
        pv = recv_until(ws, "p1")["result"]
        assert pv["db"] and pv["db"]["ok"] is True
        assert pv["contains_secrets"] is True
        assert pv["skills"] == ["demo"]

        ws.send_json({"id": "i1", "method": "data.import_full",
                      "params": {"path": exp["path"]}})
        imp = recv_until(ws, "i1")["result"]
        assert imp["db_restored"] is True
        assert imp["skills_installed"] == ["demo"]
        assert set(imp["config_restored"]) >= {"config.toml", "memory.md"}

        # 恢复后的会话列表里能看到迁移过来的会话
        ws.send_json({"id": "l1", "method": "session.list", "params": {}})
        sessions = recv_until(ws, "l1")["result"]["sessions"]
        assert len(sessions) >= 1

    # 逐项核对落盘内容：配置含可用的 Key（导入后明文会被本机 DPAPI 重新加密，
    # 同机测试里断言两种形态都认）、记忆与技能正文俱在、会话消息真的进了新库
    cfg_text = (home_b / "config.toml").read_text(encoding="utf-8")
    assert "deepseek" in cfg_text
    assert "sk-migration-key" in cfg_text or "dpapi:" in cfg_text
    assert "迁移前记忆" in (home_b / "memory.md").read_text(encoding="utf-8")
    assert (home_b / "skills" / "demo" / "SKILL.md").is_file()
    conn = sqlite3.connect(home_b / "skysheep.db")
    try:
        rows = conn.execute("SELECT content FROM messages WHERE role = 'user'").fetchall()
    finally:
        conn.close()
    assert any("跨机迁移测试消息" in r[0] for r in rows)


def test_import_full_rejects_db_from_newer_version(home):
    """新版程序创建的库：预览就标红，导入拒绝，本机数据原样。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text("[providers.old]\napi_key = 'keep'\n", encoding="utf-8")

    newer = home / "newer.db"
    conn = sqlite3.connect(newer)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 3}")
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    finally:
        conn.close()
    zip_path = home / "pkg-newer.zip"
    _write_zip(zip_path, {
        "skysheep.db": newer.read_bytes(),
        "config.toml": b"[providers.new]\napi_key = 'x'\n",
    })

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "data.preview_import_full",
                      "params": {"path": str(zip_path)}})
        pv = recv_until(ws, "p1")["result"]
        assert pv["db"]["ok"] is False and "更新版本" in pv["db"]["reason"]

        ws.send_json({"id": "i1", "method": "data.import_full",
                      "params": {"path": str(zip_path)}})
        bad = recv_until(ws, "i1")
        assert not bad["ok"] and "更新版本" in bad["error"]

    # 本机 config 未被动过（先验后换：坏包根本不进主库位置）
    assert "keep" in (home_dir / "config.toml").read_text(encoding="utf-8")


def test_import_full_rejects_corrupt_db(home):
    """撕裂/垃圾字节的库：预览报可读原因，导入拒绝。"""
    from test_server import make_client, recv_until

    zip_path = home / "pkg-corrupt.zip"
    _write_zip(zip_path, {
        "skysheep.db": b"this is not a sqlite database at all",
        "config.toml": b"[providers.x]\napi_key = 'x'\n",
    })
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "data.preview_import_full",
                      "params": {"path": str(zip_path)}})
        pv = recv_until(ws, "p1")["result"]
        assert pv["db"]["ok"] is False and pv["db"]["reason"]

        ws.send_json({"id": "i1", "method": "data.import_full",
                      "params": {"path": str(zip_path)}})
        assert not recv_until(ws, "i1")["ok"]


def test_import_full_rolls_back_when_config_invalid(home):
    """库换上后配置校验失败：会话库回滚到导入前，原会话仍然可用。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text("[providers.old]\napi_key = 'old'\n", encoding="utf-8")

    # 先用合法流程导出一份含库的包，再把包里的 config.toml 换成非法 TOML
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "导入前的会话"}})
        recv_until(ws, "c1")
        ws.send_json({"id": "e1", "method": "data.export_full", "params": {}})
        exp = recv_until(ws, "e1")["result"]
    src = zipfile.ZipFile(exp["path"])
    files = {n: src.read(n) for n in src.namelist() if n != "README.txt"}
    src.close()
    files["config.toml"] = b"not [valid toml"
    tampered = home / "pkg-badcfg.zip"
    tampered.write_bytes(_zip_bytes(files))

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "data.import_full",
                      "params": {"path": str(tampered)}})
        bad = recv_until(ws, "i1")
        assert not bad["ok"] and "校验不通过" in bad["error"]

        # 校验失败发生在任何写盘之前：库未被动过，原会话照常可查
        ws.send_json({"id": "l1", "method": "session.list", "params": {}})
        sessions = recv_until(ws, "l1")["result"]["sessions"]
        assert len(sessions) >= 1
        assert "old" in (home_dir / "config.toml").read_text(encoding="utf-8")


def test_settings_import_rolls_back_on_write_failure(home):
    """设置导入：写入中途失败（memory.md 落点是目录）→ 整体还原，不留半成品。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    old_cfg = "[providers.old]\napi_key = 'old'\n"
    (home_dir / "config.toml").write_text(old_cfg, encoding="utf-8")
    # 失败注入：memory.md 的落点是个目录 → write 必失败。
    # （不用 ui.json：应用启动本身就要写 ui.json，会把服务都拦在 boot 之外）
    (home_dir / "memory.md").mkdir()

    zip_path = home / "pkg-settings.zip"
    _write_zip(zip_path, {
        "config.toml": b"[providers.new]\napi_key = 'new'\n",
        "memory.md": "- 新记忆\n".encode(),
        "ui.json": json.dumps({"theme": "纸墨"}).encode("utf-8"),
    })

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # boot 时 load_config 会把明文 Key 升级为 DPAPI 密文并回写，所以导入前的
        # 磁盘字节要在连接建立后取——回滚断言比对的就是这份「当时」的样子
        pre_cfg = (home_dir / "config.toml").read_bytes()
        pre_ui = (home_dir / "ui.json").read_bytes() if (home_dir / "ui.json").is_file() else None
        ws.send_json({"id": "i1", "method": "settings.import",
                      "params": {"b64": __import__("base64").b64encode(
                          zip_path.read_bytes()).decode("ascii")}})
        bad = recv_until(ws, "i1")
        assert not bad["ok"] and "回滚" in bad["error"]

    # 全有或全无：config 回到导入前的字节；写在失败点之后的 ui.json 也没落包里的内容
    assert (home_dir / "config.toml").read_bytes() == pre_cfg
    post_ui = (home_dir / "ui.json").read_bytes() if (home_dir / "ui.json").is_file() else None
    assert post_ui == pre_ui


def test_settings_import_rejects_invalid_config_before_write(home):
    """包里的 config.toml 不是合法 TOML：一个字节都不落盘。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    old_cfg = "[providers.old]\napi_key = 'old'\n"
    (home_dir / "config.toml").write_text(old_cfg, encoding="utf-8")

    zip_path = home / "pkg-badtoml.zip"
    _write_zip(zip_path, {
        "config.toml": b"not [valid toml",
        "memory.md": "- 不该出现的记忆\n".encode(),
    })

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "settings.import",
                      "params": {"b64": __import__("base64").b64encode(
                          zip_path.read_bytes()).decode("ascii")}})
        bad = recv_until(ws, "i1")
        assert not bad["ok"] and "TOML" in bad["error"]

    assert (home_dir / "config.toml").read_text(encoding="utf-8") == old_cfg
    assert not (home_dir / "memory.md").exists()


def test_import_full_rolls_back_when_second_config_write_fails(home, monkeypatch):
    """第二项配置写失败：已写入的第一项必须还原（回滚快照不能丢）。

    旧实现的快照是「边写边建、随函数返回才交给调用方」：第二项写失败时
    第一项的快照没有传播出去，回滚拿到空字典，接口仍称「已回滚」但原
    config.toml 已被覆盖。这里用真实 data.import_full 入口钉住行为。
    注入点对齐现实现：配置恢复走 textio 的原子写入口（backend 模块内绑定
    名），不再直接调 Path.write_bytes。
    """
    from test_server import make_client, recv_until

    import skysheep.server.backend as backend_mod
    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    original = "[providers.old]\napi_key = 'old'\n"
    (home_dir / "config.toml").write_text(original, encoding="utf-8")

    zip_path = home / "pkg-two-cfg.zip"
    _write_zip(zip_path, {
        "config.toml": b"[providers.new]\napi_key = 'new'\n",
        "memory.md": "- 新记忆\n".encode(),
    })

    # 只让 memory.md 的写入失败（模拟磁盘/权限错误），config.toml 正常写入
    real_atomic = backend_mod.write_bytes_atomic
    atomic_calls: list[str] = []

    def flaky_atomic(path, data, **kw):
        atomic_calls.append(path.name)
        if path.name == "memory.md":
            raise PermissionError(13, "permission denied (probe)")
        return real_atomic(path, data, **kw)

    monkeypatch.setattr(backend_mod, "write_bytes_atomic", flaky_atomic)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "data.import_full",
                      "params": {"path": str(zip_path)}})
        bad = recv_until(ws, "i1")
        assert not bad["ok"], bad
        assert "已回滚" in bad["error"]

    # 关键断言：已写入的 config.toml 必须回到导入前内容
    assert (home_dir / "config.toml").read_text(encoding="utf-8") == original
    # 回滚同样走原子入口：config.toml 写了两次（恢复一次 + 回滚还原一次），
    # memory.md 只在恢复时失败过一次（回滚走 unlink，不经过原子写）
    assert atomic_calls.count("config.toml") == 2
    assert atomic_calls.count("memory.md") == 1


def test_import_full_rolls_back_skills_on_late_failure(home, monkeypatch):
    """技能复制阶段失败：已复制的技能目录要撤销，新技能不残留。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text("[providers.old]\napi_key = 'old'\n", encoding="utf-8")
    zip_path = home / "pkg-skills.zip"
    _write_zip(zip_path, {
        "skills/demo-a/SKILL.md": b"---\nname: demo-a\ndescription: A\n---\n",
        "skills/demo-b/SKILL.md": b"---\nname: demo-b\ndescription: B\n---\n",
    })

    import skysheep.skills.installer as installer_mod
    real_target = installer_mod._skill_target

    def flaky_target(root, name):
        if name == "demo-b":
            raise installer_mod.SkillInstallError("模拟技能名校验失败")
        return real_target(root, name)

    monkeypatch.setattr(installer_mod, "_skill_target", flaky_target)
    try:
        with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "i1", "method": "data.import_full",
                          "params": {"path": str(zip_path)}})
            bad = recv_until(ws, "i1")
            assert not bad["ok"], bad
            assert "已回滚" in bad["error"]
    finally:
        monkeypatch.setattr(installer_mod, "_skill_target", real_target)

    assert not (home_dir / "skills" / "demo-a").exists(), "已复制的技能应被回滚删除"
    assert not (home_dir / "skills" / "demo-b").exists()
