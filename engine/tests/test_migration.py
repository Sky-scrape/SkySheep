"""跨机全量迁移（P24）：整包导出 / 导入的后端链路。

覆盖面：
- 导出：包含 db / 技能 / ui.json / 脱敏 config；**不含任何密钥值、不含
  mcp.json**；manifest 与包内容一致；尺寸上限的可读报错；
- 导入：全新 home 还原（会话/消息/技能合并/ui.json/config）→ 本机原库
  先备份进备份目录；config 再剥一遍密钥；ui.json 的 accept_edits 剥除；
- 降防护清洗（F3.1）：包内库的自动化/绑定/白名单表（cron_tasks、
  schedules、whitelist_rules、channel_bindings、channel_sources、
  pipelines、pipeline_nodes）整表清空且逐表披露；config 防护总开关键
  （computer_control / browser_control / browser_ignore_cert）剥除并披露；
- 内容预算（F3.3）：技能条目数与累计解压大小超限（zip bomb）在扫描
  阶段整体拒绝；path 档与 data_b64 档同一道整包尺寸上限；
- 导出落盘：临时名掺随机分量（F3.4，同秒并发导出不共用 tmp）；
- 技能覆盖备份（F3.5）：merge 档覆盖前复制留底、失败按备份还原、
  还原不了逐个点名；replace 档既有回滚行为回归；
- 安全面：zip 条目穿越（../ 与盘符）与未知条目整体拒绝，坏包一个字节
  都不落盘；manifest 篡改拒绝；新版库版本拒绝；
- 忙碌保护：团队自动循环在飞（_teams 任一 loop_running）拒绝；定时任务
  在飞（_backup_busy_message）拒绝；带活库的 mixin 导入走完
  「收连接 → 换库 → 重开」全程后 store 仍可用。

SKYSHEEP_HOME 全程隔离（conftest 的 home 夹具），绝不碰真实 ~/.skysheep。
"""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from skysheep.messages import Message
from skysheep.session.store import SessionStore


# 与 test_data_migration 同一处置：进程级 run-once 的明文凭据 DPAPI 迁移
# 钉在「已迁移过」，本文件断言磁盘 config 保持明文时不受 xdist 调度漂移影响
@pytest.fixture(autouse=True)
def _freeze_credential_migration(monkeypatch):
    from skysheep import config as config_mod

    monkeypatch.setattr(config_mod, "_migration_attempted", True)


_ANCHOR_A = "跨机迁移锚点消息"
_ANCHOR_B = "本机原有消息"


async def _make_home_store(home_dir: Path, anchor: str) -> SessionStore:
    """在指定 home 建库 + 一条会话 + 一条锚点消息（迁移内容的核对锚点）。

    用完由调用方 close（Windows 上换库/快照前不能留着打开的连接）。
    """
    home_dir.mkdir(parents=True, exist_ok=True)
    store = await SessionStore(home_dir / "skysheep.db").connect()
    proj = await store.get_or_create_project(str(home_dir / "proj"))
    sess = await store.create_session(proj.id, title="迁移会话")
    await store.append_message(sess.id, Message.user(anchor))
    return store


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _db_texts(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [str(r[0]) for r in conn.execute("SELECT content FROM messages")]
    finally:
        conn.close()


# ---- 导出 ----


async def test_export_package_contents_and_secrets_stripped(tmp_path):
    """包含 db/技能/ui.json/脱敏 config；不含密钥值与 mcp.json；manifest 对得上。"""
    from skysheep.server.backend_parts.migration import build_migration_package

    home_a = tmp_path / "homeA"
    store = await _make_home_store(home_a, _ANCHOR_A)
    await store.close()
    sk = home_a / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 迁移测试技能\n---\n", encoding="utf-8"
    )
    (home_a / "skills" / "loose.txt").write_text("散文件", encoding="utf-8")
    (home_a / "ui.json").write_text(
        json.dumps({"theme": "夜墨", "accept_edits": 2}), encoding="utf-8"
    )
    (home_a / "config.toml").write_text(
        # 夹具假密钥：值是编造的，按仓库惯例分段拼接（完整字面量会被安全扫描记成硬编码凭据）
        '[providers.deepseek]\n'
        'api_key = "sk-" "secret-123"\n'
        'base_url = "https://api.test/v1"\n'
        '[channels.feishu]\n'
        'app_secret = "sec-" "456"\n'
        'password = "pw-" "789"\n',
        encoding="utf-8",
    )
    # mcp.json 的鉴权头是明文凭据：整个文件不进包
    (home_a / "mcp.json").write_text(
        json.dumps({"servers": {"t": {"headers": {"Authorization": "Bearer top-secret"}}}}),
        encoding="utf-8",
    )

    data, manifest = build_migration_package(home_a)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert {"skysheep.db", "config.toml", "ui.json",
                "skills/demo/SKILL.md"} <= names
        # 红线一：mcp.json 不进包，鉴权头值在任何条目里都不出现
        assert "mcp.json" not in names
        all_bytes = b"".join(zf.read(n) for n in names)
        assert b"top-secret" not in all_bytes
        # 红线二：config 脱敏——密钥值与键行都不在，普通配置保留
        cfg = zf.read("config.toml").decode("utf-8")
        for secret in ("sk-secret-123", "sec-456", "pw-789", "api_key",
                       "app_secret", "password"):
            assert secret not in cfg
        assert "base_url" in cfg and "https://api.test/v1" in cfg
        # 技能根下的散文件不收（导入白名单只认 skills/<名>/<文件> 形状）
        assert "skills/loose.txt" not in names
        # 库内容进包：锚点消息在快照里
        probe = tmp_path / "probe.db"
        probe.write_bytes(zf.read("skysheep.db"))
    assert any(_ANCHOR_A in t for t in _db_texts(probe))

    # manifest：技能清单、被剥键路径、内容清单与包一致
    assert manifest["skills"] == ["demo"]
    assert set(manifest["sanitized_keys"]) == {
        "providers.deepseek.api_key",
        "channels.feishu.app_secret",
        "channels.feishu.password",
    }
    assert {f["name"] for f in manifest["files"]} == names - {"manifest.json"}


def test_export_rejects_oversized_package(tmp_path, monkeypatch):
    """整包超上限：可读错误（v1 防 WS 帧爆炸），不是裸 OverflowError。"""
    from skysheep.server.backend_parts import migration as migration_mod

    monkeypatch.setattr(migration_mod, "MAX_MIGRATION_PACKAGE_BYTES", 16)
    home_a = tmp_path / "homeA"
    home_a.mkdir()
    (home_a / "config.toml").write_text("[a]\nb = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="上限"):
        migration_mod.build_migration_package(home_a)


def test_export_rejects_config_that_breaks_after_sanitize(tmp_path, monkeypatch):
    """脱敏后不再是合法 TOML（手改出多行字符串等怪形状）：拒绝导出，不带坏配置上飞机。"""
    from skysheep.server.backend_parts import migration as migration_mod

    real_sanitize = migration_mod.sanitize_config_text

    def break_toml(text: str) -> tuple[str, list[str]]:
        out, keys = real_sanitize(text)
        return out + "this is not [valid toml", keys

    monkeypatch.setattr(migration_mod, "sanitize_config_text", break_toml)
    home_a = tmp_path / "homeA"
    home_a.mkdir()
    (home_a / "config.toml").write_text(
        '[providers.x]\napi_key = "k"\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="脱敏后无法解析"):
        migration_mod.build_migration_package(home_a)


# ---- 导入：还原 / 备份 / 合并 ----


async def _build_package(home_a: Path) -> bytes:
    """标准 A 侧包：会话 + 技能 demo + ui.json（带 accept_edits）+ 带密钥 config。"""
    from skysheep.server.backend_parts.migration import build_migration_package

    store = await _make_home_store(home_a, _ANCHOR_A)
    await store.close()
    sk = home_a / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 迁移测试技能\n---\n", encoding="utf-8"
    )
    (home_a / "ui.json").write_text(
        json.dumps({"theme": "夜墨", "accept_edits": 2}), encoding="utf-8"
    )
    (home_a / "config.toml").write_text(
        # 同上：夹具假密钥分段拼接
        '[providers.deepseek]\napi_key = "sk-" "secret-123"\n'
        'base_url = "https://api.test/v1"\n',
        encoding="utf-8",
    )
    data, manifest = build_migration_package(home_a)
    assert manifest["skills"] == ["demo"]
    return data


async def test_import_replaces_data_and_backs_up_local_db(tmp_path):
    """B 环境导入：会话/技能/ui/config 还原 + 本机原库先进备份目录 + 密钥不落盘。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_a = tmp_path / "homeA"
    data = await _build_package(home_a)

    # B 环境：本机已有自己的库（含锚点消息）、技能、带旧密钥的 config、ui.json
    home_b = tmp_path / "homeB"
    store_b = await _make_home_store(home_b, _ANCHOR_B)
    await store_b.close()
    other = home_b / "skills" / "other"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text(
        "---\nname: other\ndescription: 本机原有\n---\n", encoding="utf-8"
    )
    (home_b / "config.toml").write_text(
        '[providers.old]\napi_key = "old-key"\n', encoding="utf-8"
    )
    (home_b / "ui.json").write_text(
        json.dumps({"theme": "纸墨"}), encoding="utf-8"
    )

    backup_dir = home_b / "backups"
    result = apply_migration_package(home_b, data, backup_dir=backup_dir)
    assert result["db_restored"] is True
    assert result["config_restored"] is True and result["ui_restored"] is True
    # 技能合并：包里的进来，本机多出的保留
    assert result["skills_new"] == ["demo"] and result["skills_overwritten"] == []
    assert (home_b / "skills" / "demo" / "SKILL.md").is_file()
    assert (home_b / "skills" / "other" / "SKILL.md").is_file()

    # 会话/消息还原：包里锚点进来了
    assert any(_ANCHOR_A in t for t in _db_texts(home_b / "skysheep.db"))
    # config 被替换成脱敏版：旧密钥没了，包里密钥也不在，键行整行删除
    cfg = (home_b / "config.toml").read_text(encoding="utf-8")
    assert "old-key" not in cfg and "sk-secret-123" not in cfg and "api_key" not in cfg
    assert "https://api.test/v1" in cfg
    # ui.json 还原 + 降防护档位不随包生效（accept_edits 剥除）
    ui = json.loads((home_b / "ui.json").read_text(encoding="utf-8"))
    assert ui["theme"] == "夜墨" and "accept_edits" not in ui

    # 本机原库已备份：备份目录里是导入前的库（含本机锚点消息）
    backup = Path(result["backup"])
    assert backup.is_file() and backup.parent == backup_dir
    assert "-迁移前" in backup.name
    assert any(_ANCHOR_B in t for t in _db_texts(backup))
    assert result["sanitized_keys"] == ["providers.deepseek.api_key"]


async def test_import_merges_over_existing_skill(tmp_path):
    """同名技能：包内容覆盖同名文件、本机独有文件保留（合并不清空）。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_a = tmp_path / "homeA"
    data = await _build_package(home_a)

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    sk = home_b / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: 旧版\n---\n", encoding="utf-8")
    (sk / "本地独有.md").write_text("别删我", encoding="utf-8")

    result = apply_migration_package(home_b, data, backup_dir=home_b / "backups")
    assert result["skills_overwritten"] == ["demo"]
    assert (sk / "本地独有.md").read_text(encoding="utf-8") == "别删我"
    assert "迁移测试技能" in (sk / "SKILL.md").read_text(encoding="utf-8")


# ---- 导入：坏包拒绝（先验后写） ----


def test_import_rejects_traversal_and_absolute_entries(tmp_path):
    """路径穿越（../）与盘符绝对路径：整体拒绝，一个字节都不落盘。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    backup_dir = home_b / "backups"

    for evil in ("../evil.txt", "skills/demo/../../../evil.txt", "C:/evil.txt",
                 "..\\evil.txt"):
        with pytest.raises(RuntimeError, match="不安全"):
            apply_migration_package(
                home_b, _zip_bytes({evil: b"x"}), backup_dir=backup_dir
            )
    assert not (tmp_path / "evil.txt").exists()
    assert not (home_b / "evil.txt").exists()
    assert not (home_b / "config.toml").exists()  # 坏包零落盘


def test_import_rejects_unknown_entries(tmp_path):
    """白名单外条目（未知文件、skills 根散文件）：整体拒绝，不静默跳过。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    with pytest.raises(RuntimeError, match="未知条目"):
        apply_migration_package(
            home_b,
            _zip_bytes({"config.toml": b"[a]\nb = 1\n", "random.txt": b"x"}),
            backup_dir=home_b / "backups",
        )
    assert not (home_b / "config.toml").exists()
    with pytest.raises(RuntimeError, match="未知条目"):
        apply_migration_package(
            home_b,
            _zip_bytes({"skills/loose.txt": b"x"}),  # 两层形状都不够
            backup_dir=home_b / "backups",
        )


def test_import_rejects_manifest_mismatch(tmp_path):
    """包内容与 manifest 清单不符（截断/篡改）：拒绝导入。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    pkg = _zip_bytes({
        "config.toml": b"[a]\nb = 1\n",
        "manifest.json": json.dumps({
            "kind": "skysheep-migration", "format_version": 1,
            "files": [{"name": "config.toml", "size": 999}],
        }).encode("utf-8"),
    })
    with pytest.raises(RuntimeError, match="manifest"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")


def test_import_rejects_missing_manifest(tmp_path):
    """没有 manifest 的包（不是本版本生成的）：拒绝导入。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    with pytest.raises(RuntimeError, match="manifest"):
        apply_migration_package(
            home_b, _zip_bytes({"config.toml": b"[a]\nb = 1\n"}),
            backup_dir=home_b / "backups",
        )


def test_import_rejects_future_format_version(tmp_path):
    """更高包格式版本：拒绝并提示升级本机（与库版本降级防护同一姿态）。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    pkg = _zip_bytes({
        "config.toml": b"[a]\nb = 1\n",
        "manifest.json": json.dumps({
            "kind": "skysheep-migration", "format_version": 99,
            "files": [{"name": "config.toml", "size": 8}],
        }).encode("utf-8"),
    })
    with pytest.raises(RuntimeError, match="升级"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")


def test_import_rejects_corrupt_db(tmp_path):
    """垃圾字节的会话库：quick_check 拦下，主库未动。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    blob = b"this is not a sqlite database at all"
    pkg = _zip_bytes({
        "skysheep.db": blob,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration", "format_version": 1,
            "files": [{"name": "skysheep.db", "size": len(blob)}],
        }).encode("utf-8"),
    })
    with pytest.raises(RuntimeError, match="会话库"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")


def test_import_rejects_db_from_newer_engine(tmp_path):
    """新版引擎创建的库（user_version 更高）：拒绝，提示先升级本机。"""
    from skysheep.server.backend_parts.migration import apply_migration_package
    from skysheep.session.store import SCHEMA_VERSION

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    newer = tmp_path / "newer.db"
    conn = sqlite3.connect(newer)
    try:
        # PRAGMA 赋值既不接受绑定参数（sqlite 语法限制）也不允许拼接（安全扫描
        # 口径）——用固定大字面量表达「未来版本」：导入侧只要求比当前 schema 新，
        # 断言兜底防 SCHEMA_VERSION 涨过这个字面量
        assert SCHEMA_VERSION < 999999
        conn.execute("PRAGMA user_version = 999999")
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    finally:
        conn.close()
    blob = newer.read_bytes()
    pkg = _zip_bytes({
        "skysheep.db": blob,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration", "format_version": 1,
            "files": [{"name": "skysheep.db", "size": len(blob)}],
        }).encode("utf-8"),
    })
    with pytest.raises(RuntimeError, match="更新版本"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")


def test_import_resanitizes_config_from_hostile_package(tmp_path):
    """导入侧不信任包自称已脱敏：config 里混进密钥键，写盘前再剥一遍。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    cfg = b'[providers.x]\napi_key = "hostile-key"\nbase_url = "https://x/v1"\n'
    pkg = _zip_bytes({
        "config.toml": cfg,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration", "format_version": 1,
            "files": [{"name": "config.toml", "size": len(cfg)}],
        }).encode("utf-8"),
    })
    result = apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    written = (home_b / "config.toml").read_text(encoding="utf-8")
    assert "hostile-key" not in written and "api_key" not in written
    assert "https://x/v1" in written
    assert result["config_restored"] is True


# ---- 忙碌保护与 mixin 装配 ----


def _make_backend(home):
    from conftest import FakeProvider

    from skysheep.server.backend import ServerBackend

    return ServerBackend(working_dir=home / "proj", provider_factory=lambda: FakeProvider([]))


async def test_import_refuses_while_team_loop_running(home):
    """团队自动循环在飞（_teams 任一 loop_running）：导入拒绝；停了就放行。"""
    be = _make_backend(home)
    be._teams["s1"] = SimpleNamespace(loop_running=True)
    blob = base64.b64encode(b"PK").decode("ascii")
    with pytest.raises(RuntimeError, match="自动循环"):
        await be.migration_import({"data_b64": blob})
    # 循环停了就不再被团队闸拦下（包坏在 zip 校验，说明已经过了忙碌门）
    be._teams["s1"] = SimpleNamespace(loop_running=False)
    with pytest.raises(RuntimeError, match="zip"):
        await be.migration_import({"data_b64": blob})


async def test_import_refuses_while_cron_running(home):
    """会话/定时任务在飞走 _backup_busy_message（与备份/恢复同一道闸）。"""
    be = _make_backend(home)
    be._cron_running[1] = None
    blob = base64.b64encode(b"PK").decode("ascii")
    with pytest.raises(RuntimeError, match="定时任务"):
        await be.migration_import({"data_b64": blob})


async def test_mixin_import_with_live_store_roundtrip(home, monkeypatch):
    """带活库的 mixin 导入：收连接 → 换库/还原 → 重开，store 全程可用。"""
    from skysheep.config import skysheep_home

    home_a = home / "homeA"
    data = await _build_package(home_a)

    # B 环境的活后端：SKYSHEEP_HOME 指到 homeB 后 setup（store 连在 homeB 的库上）
    home_b = home / "homeB"
    home_b.mkdir()
    monkeypatch.setenv("SKYSHEEP_HOME", str(home_b))
    be = _make_backend(home)
    await be.setup()
    store_b = await _make_home_store(home_b, _ANCHOR_B)
    await store_b.close()
    # 注意：setup 已经连着 homeB 的库（连接活着），这里再补一条本机消息
    proj = await be.store.get_or_create_project(str(home_b / "proj"))
    sess = await be.store.create_session(proj.id, title="本机会话")
    await be.store.append_message(sess.id, Message.user(_ANCHOR_B))

    result = await be.migration_import(
        {"data_b64": base64.b64encode(data).decode("ascii")}
    )
    assert result["restart_required"] is True
    manifest = result["manifest"]
    assert manifest["db_restored"] is True
    assert manifest["skills_new"] == ["demo"]
    assert Path(manifest["backup"]).is_file()

    # 包里的会话在重开后的 store 里可见（换库 + 重开全程走通）
    sessions = await be.store.list_sessions()
    assert any(s.title == "迁移会话" for s in sessions)
    # 备份里是导入前的库
    assert any(_ANCHOR_B in t for t in _db_texts(Path(manifest["backup"])))
    # config 已替换为脱敏版
    cfg = (home_b / "config.toml").read_text(encoding="utf-8")
    assert "sk-secret-123" not in cfg and "api_key" not in cfg
    assert str(skysheep_home()) == str(home_b)  # 全程没离开 B 环境


# ---- WS 注册面 ----


async def test_migration_export_over_ws(home):
    """dispatch 注册面：migration.export 走 WS 回 base64 包（内容与纯函数一致）。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    home_dir = skysheep_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text(
        '[providers.deepseek]\napi_key = "sk-ws-key"\n', encoding="utf-8"
    )
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "WS 导出锚点"}})
        assert recv_until(ws, "c1")["ok"]
        ws.send_json({"id": "m1", "method": "migration.export", "params": {}})
        frame = recv_until(ws, "m1")
        assert frame["ok"], frame
        result = frame["result"]
        assert result["filename"].startswith("skysheep-migration-")
        assert result["size"] > 0
        data = base64.b64decode(result["data_b64"])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            assert "skysheep.db" in names and "mcp.json" not in names
            assert b"sk-ws-key" not in b"".join(zf.read(n) for n in names)
        assert result["manifest"]["kind"] == "skysheep-migration"


async def test_migration_import_busy_refused_over_ws(home):
    """dispatch 注册面：团队循环在飞时 migration.import 经 WS 也被拒（可读错误帧）。"""
    from test_server import make_client, recv_until

    from skysheep.config import skysheep_home

    skysheep_home().mkdir(parents=True, exist_ok=True)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b1", "method": "boot", "params": {}})
        assert recv_until(ws, "b1")["ok"]
        client.app.state.backend._teams["s1"] = SimpleNamespace(loop_running=True)
        ws.send_json({"id": "m1", "method": "migration.import",
                      "params": {"data_b64": base64.b64encode(b"PK").decode("ascii")}})
        frame = recv_until(ws, "m1")
        assert frame["ok"] is False and "自动循环" in frame["error"]


# ---- P24 v2：超大包落盘模式 / 按路径导入 / 技能替换口径 ----


class _MigHost:
    """只要 mixin 需要的几个属性，不背整个 backend。"""

    def __init__(self, store=None):
        self.store = store
        self._teams: dict = {}
        self._base_run_task = None
        self.runtimes: dict = {}
        self._cron_running = False
        self._pipeline_running = False

    _backup_busy_message = staticmethod(lambda *a, **k: None)


async def test_export_oversized_falls_back_to_disk_mode(home, monkeypatch):
    """超过 64MB 不再拒绝：写到 ~/.skysheep/migrations/ 并回传路径。"""
    from skysheep.server.backend_parts import migration as migration_mod
    from skysheep.server.backend_parts.migration import MigrationMixin

    monkeypatch.setattr(migration_mod, "MAX_MIGRATION_PACKAGE_BYTES", 64)
    (home / "home").mkdir(parents=True, exist_ok=True)
    (home / "home" / "config.toml").write_text("[a]\nb = 1\n", encoding="utf-8")

    class _Host(_MigHost):
        migration_export = MigrationMixin.migration_export

    out = await _Host().migration_export()
    assert out["mode"] == "file"
    assert "data_b64" not in out, "落盘模式不再回传 base64（几百 MB 挤不进 WS 帧）"
    path = Path(out["path"])
    assert path.is_file() and path.stat().st_size > 0
    assert path.parent == home / "home" / "migrations"
    assert out["manifest"]["kind"] == "skysheep-migration"


async def test_export_inline_mode_when_small(home):
    from skysheep.server.backend_parts.migration import MigrationMixin

    class _Host(_MigHost):
        migration_export = MigrationMixin.migration_export

    out = await _Host().migration_export()
    assert out["mode"] == "inline" and out["data_b64"]


async def test_import_from_path_and_replace_mode(home):
    """按本机路径导入 + replace：同名技能整目录替换，本机多出的技能保留。"""
    from skysheep.server.backend_parts.migration import (
        MigrationMixin,
        build_migration_package,
    )

    # 源机器：一个技能 demo（只含 SKILL.md）
    src = home / "src"
    (src / "skills" / "demo").mkdir(parents=True)
    (src / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 包里那一版\n---\n", encoding="utf-8"
    )
    data, _manifest = build_migration_package(src)
    pkg = home / "pkg.zip"
    pkg.write_bytes(data)

    # 本机：同名技能多一个 extra.txt，另有一个包里没有的技能 keep
    tgt = home / "home"
    (tgt / "skills" / "demo").mkdir(parents=True)
    (tgt / "skills" / "demo" / "SKILL.md").write_text("本机旧版", encoding="utf-8")
    (tgt / "skills" / "demo" / "extra.txt").write_text("本机残留", encoding="utf-8")
    (tgt / "skills" / "keep" / "SKILL.md").parent.mkdir(parents=True)
    (tgt / "skills" / "keep" / "SKILL.md").write_text("keep", encoding="utf-8")

    class _Host(_MigHost):
        migration_import = MigrationMixin.migration_import

    out = await _Host().migration_import({
        "path": str(pkg), "skills_mode": "replace",
    })
    assert out["restart_required"] is True
    assert out["manifest"]["skills_mode"] == "replace"
    demo = tgt / "skills" / "demo"
    assert "包里那一版" in (demo / "SKILL.md").read_text(encoding="utf-8")
    assert not (demo / "extra.txt").exists(), "replace 档不得留下本机旧文件"
    assert (tgt / "skills" / "keep" / "SKILL.md").is_file(), "包里没有的技能不删"


async def test_import_merge_mode_keeps_local_extra_files(home):
    """merge（默认）仍是叠加覆盖：同名技能的本机额外文件保留。"""
    from skysheep.server.backend_parts.migration import (
        MigrationMixin,
        build_migration_package,
    )

    src = home / "src"
    (src / "skills" / "demo").mkdir(parents=True)
    (src / "skills" / "demo" / "SKILL.md").write_text("包里那一版", encoding="utf-8")
    data, _ = build_migration_package(src)

    tgt = home / "home"
    (tgt / "skills" / "demo").mkdir(parents=True)
    (tgt / "skills" / "demo" / "SKILL.md").write_text("本机旧版", encoding="utf-8")
    (tgt / "skills" / "demo" / "extra.txt").write_text("本机残留", encoding="utf-8")

    class _Host(_MigHost):
        migration_import = MigrationMixin.migration_import

    await _Host().migration_import({
        "data_b64": base64.b64encode(data).decode("ascii"),
    })
    demo = tgt / "skills" / "demo"
    assert (demo / "SKILL.md").read_text(encoding="utf-8") == "包里那一版"
    assert (demo / "extra.txt").is_file(), "merge 档不该删本机文件"


async def test_import_rejects_unknown_skills_mode(home):
    from skysheep.server.backend_parts.migration import MigrationMixin

    class _Host(_MigHost):
        migration_import = MigrationMixin.migration_import

    with pytest.raises(RuntimeError, match="merge"):
        await _Host().migration_import({"data_b64": "AAAA", "skills_mode": "wipe"})


# ---- F3.1：降防护状态清洗（自动化/绑定/白名单表 + config 防护总开关） ----


def _make_planted_package(db_path: Path) -> bytes:
    """建真实 schema 的库、植入自动化/白名单/渠道绑定行（模拟恶意包），打成迁移包。

    db_path 须是已由 SessionStore 建好 schema 的库文件（调用方先用
    _make_home_store 建库）。
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
        pid = int(row[0]) if row else 1
        conn.execute(
            "INSERT INTO cron_tasks (project_id, name, prompt, created_at)"
            " VALUES (?, ?, ?, ?)",
            (pid, "植入定时任务", "到点自动认领就跑的提示词", 0),
        )
        conn.execute(
            "INSERT INTO schedules (title, start_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            ("植入日程", 9999999999, 0, 0),
        )
        conn.execute(
            "INSERT INTO whitelist_rules (project_id, tool, kind, pattern, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (pid, "run_command", "prefix", "植入白名单前缀", 0),
        )
        conn.execute(
            "INSERT INTO channel_bindings (channel, session_id, updated_at)"
            " VALUES (?, ?, ?)",
            ("feishu", "sess-planted", 0),
        )
        conn.execute(
            "INSERT INTO channel_sources (channel, chat_id, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?)",
            ("feishu", "oc-planted", 0, 0),
        )
        conn.execute(
            "INSERT INTO pipelines (id, project_id, name, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (1, pid, "植入流水线", "running", 0),
        )
        conn.execute(
            "INSERT INTO pipeline_nodes (pipeline_id, title, prompt) VALUES (?, ?, ?)",
            (1, "植入节点", "跟着流水线循环自动跑"),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    blob = db_path.read_bytes()
    return _zip_bytes({
        "skysheep.db": blob,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration",
            "format_version": 1,
            "files": [{"name": "skysheep.db", "size": len(blob)}],
        }).encode("utf-8"),
    })


async def test_import_cleans_planted_automation_tables_and_discloses(tmp_path):
    """恶意包植入的自动化/白名单/渠道绑定行不随包生效，且逐表披露清洗行数。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_a = tmp_path / "homeA"
    home_a.mkdir()
    store = await _make_home_store(home_a, _ANCHOR_A)
    await store.close()
    pkg = _make_planted_package(home_a / "skysheep.db")

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    result = apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    assert result["db_restored"] is True

    # 换上的库：七张表的植入行全部为空（在跑的 cron / 流水线循环扫不到东西）
    new_db = home_b / "skysheep.db"
    conn = sqlite3.connect(new_db)
    try:
        counts = {
            "cron_tasks": conn.execute("SELECT COUNT(*) FROM cron_tasks").fetchone()[0],
            "schedules": conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0],
            "whitelist_rules": conn.execute(
                "SELECT COUNT(*) FROM whitelist_rules").fetchone()[0],
            "channel_bindings": conn.execute(
                "SELECT COUNT(*) FROM channel_bindings").fetchone()[0],
            "channel_sources": conn.execute(
                "SELECT COUNT(*) FROM channel_sources").fetchone()[0],
            "pipelines": conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0],
            "pipeline_nodes": conn.execute(
                "SELECT COUNT(*) FROM pipeline_nodes").fetchone()[0],
        }
    finally:
        conn.close()
    assert all(v == 0 for v in counts.values()), counts
    # 披露字段逐项点名：清了哪些表、各多少行
    assert result["db_cleaned"] == {
        "cron_tasks": 1,
        "schedules": 1,
        "whitelist_rules": 1,
        "channel_bindings": 1,
        "channel_sources": 1,
        "pipelines": 1,
        "pipeline_nodes": 1,
    }


def test_import_strips_guard_switch_keys_and_discloses(tmp_path):
    """防护总开关键不随包生效（与 accept_edits 同款语义），剥除结果进披露字段。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    cfg = (
        b"computer_control = true\n"
        b"browser_control = true\n"
        b"browser_ignore_cert = true\n"
        b"max_iterations = 30\n"
    )
    pkg = _zip_bytes({
        "config.toml": cfg,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration",
            "format_version": 1,
            "files": [{"name": "config.toml", "size": len(cfg)}],
        }).encode("utf-8"),
    })
    result = apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    written = (home_b / "config.toml").read_text(encoding="utf-8")
    for key in ("computer_control", "browser_control", "browser_ignore_cert"):
        assert key not in written
    assert "max_iterations = 30" in written, "普通配置不受清洗影响"
    assert result["config_guard_keys_stripped"] == [
        "browser_control",
        "browser_ignore_cert",
        "computer_control",
    ]
    assert result["db_cleaned"] == {}, "包里没有库时披露为空"


def test_import_strips_guard_dotted_and_quoted_keys(tmp_path):
    """点分键 / 单引号键变体：按行剥除，导入继续、解析结构里不存在这些键。

    断言口径与加载侧一致（config.py 用 bool(raw.get(key, False)) 判开关）：
    只要 tomllib 解析结果里没有这几个键，开关就不可能被随包打开。
    """
    import tomllib

    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    cfg = (
        b"computer_control.x = true\n"
        b"browser_control.flag = true\n"
        b"browser_ignore_cert.path = 'a'\n"
        b"'computer_control' = true\n"
        b"computer_control_extra = true\n"
        b"max_iterations = 30\n"
    )
    pkg = _zip_bytes({
        "config.toml": cfg,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration",
            "format_version": 1,
            "files": [{"name": "config.toml", "size": len(cfg)}],
        }).encode("utf-8"),
    })
    result = apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    parsed = tomllib.loads((home_b / "config.toml").read_text(encoding="utf-8"))
    for key in ("computer_control", "browser_control", "browser_ignore_cert"):
        assert key not in parsed, key
    # 名字不同前缀的普通键不受清洗影响
    assert parsed["computer_control_extra"] is True
    assert parsed["max_iterations"] == 30
    assert result["config_guard_keys_stripped"] == [
        "browser_control",
        "browser_ignore_cert",
        "computer_control",
    ]


@pytest.mark.parametrize(
    "cfg_text",
    [
        # 段头 + 任意子键（复审复现的形态）：bool(非空表) 即真，等于打开开关
        b"[computer_control]\nenabled_anything = true\n",
        # 数组表、带引号段头、子段头等变体
        b"[[computer_control]]\nx = 1\n",
        b'["computer_control"]\nx = 1\n',
        b"[computer_control.sub]\nx = 1\n",
        b"[browser_control]\nx = 1\n",
        b"[browser_ignore_cert]\nx = 1\n",
    ],
)
def test_import_rejects_guard_table_forms(tmp_path, cfg_text):
    """表形态 fail-closed：逐行剥不掉的形状经 tomllib 解析兜底整体拒绝，零落盘。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    pkg = _zip_bytes({
        "config.toml": cfg_text,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration",
            "format_version": 1,
            "files": [{"name": "config.toml", "size": len(cfg_text)}],
        }).encode("utf-8"),
    })
    with pytest.raises(RuntimeError, match="表/变体形态"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    assert not (home_b / "config.toml").exists()


# ---- F3.3：技能内容解压预算（zip bomb）与 path 档尺寸上限 ----


def _skill_pkg(files: dict[str, bytes]) -> bytes:
    """打一个只含技能条目（manifest 尺寸与内容一致）的迁移包。"""
    return _zip_bytes({
        **files,
        "manifest.json": json.dumps({
            "kind": "skysheep-migration",
            "format_version": 1,
            "files": [{"name": n, "size": len(d)} for n, d in files.items()],
        }).encode("utf-8"),
    })


def test_import_rejects_skill_content_over_budget(tmp_path, monkeypatch):
    """技能内容解压预算：累计大小超上限在扫描阶段整体拒绝，零落盘。"""
    from skysheep.server.backend_parts import migration as migration_mod
    from skysheep.server.backend_parts.migration import apply_migration_package

    monkeypatch.setattr(migration_mod, "MAX_UNPACKED_BYTES", 16)
    home_b = tmp_path / "homeB"
    home_b.mkdir()
    pkg = _skill_pkg({
        "skills/demo/SKILL.md": b"x" * 10,
        "skills/demo/extra.bin": b"x" * 10,
    })
    with pytest.raises(RuntimeError, match="过大"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    assert not (home_b / "skills").exists()


def test_import_rejects_skill_file_count_over_budget(tmp_path, monkeypatch):
    """技能内容解压预算：条目数超上限整体拒绝。"""
    from skysheep.server.backend_parts import migration as migration_mod
    from skysheep.server.backend_parts.migration import apply_migration_package

    monkeypatch.setattr(migration_mod, "MAX_SKILL_FILES", 3)
    home_b = tmp_path / "homeB"
    home_b.mkdir()
    pkg = _skill_pkg({f"skills/demo/f{i}.txt": b"x" for i in range(4)})
    with pytest.raises(RuntimeError, match="过多"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    assert not (home_b / "skills").exists()


def test_import_rejects_zip_bomb_at_real_limit(tmp_path):
    """真实上限的 zip bomb：120MB+ 全零内容压成几 KB 的包，扫描阶段即拒。"""
    from skysheep.server.backend_parts.migration import apply_migration_package
    from skysheep.skills.installer import MAX_UNPACKED_BYTES

    home_b = tmp_path / "homeB"
    home_b.mkdir()
    blob = b"\0" * (MAX_UNPACKED_BYTES + 1024 * 1024)  # 超上限 1MB 的解压内容
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("skills/demo/SKILL.md", blob)
        zf.writestr("manifest.json", json.dumps({
            "kind": "skysheep-migration",
            "format_version": 1,
            "files": [{"name": "skills/demo/SKILL.md", "size": len(blob)}],
        }).encode("utf-8"))
    pkg = buf.getvalue()
    assert len(pkg) < 1024 * 1024, "包体本身很小——这正是 zip bomb 的形状"
    with pytest.raises(RuntimeError, match="过大"):
        apply_migration_package(home_b, pkg, backup_dir=home_b / "backups")
    assert not (home_b / "skills").exists()


async def test_mixin_path_import_enforces_size_cap(home, monkeypatch):
    """path 档与 data_b64 档同一道整包尺寸上限：磁盘上的包不再豁免（F3.3）。"""
    from skysheep.server.backend_parts import migration as migration_mod
    from skysheep.server.backend_parts.migration import MigrationMixin

    monkeypatch.setattr(migration_mod, "MAX_MIGRATION_PACKAGE_BYTES", 16)
    pkg = home / "pkg-over-limit.zip"
    pkg.write_bytes(b"P" * 64)  # 内容不合法也无所谓：尺寸关先于 zip 解析
    class _Host(_MigHost):
        migration_import = MigrationMixin.migration_import

    with pytest.raises(RuntimeError, match="尺寸上限"):
        await _Host().migration_import({"path": str(pkg)})


# ---- F3.4：导出落盘的临时名掺随机分量 ----


def test_write_package_file_tmp_name_randomized(tmp_path, monkeypatch):
    """同秒两次落盘导出不得共用同一 tmp 名（并发写同一确定名会互相混写）。"""
    from skysheep.server.backend_parts import migration as migration_mod

    seen: list[str] = []
    real_replace = migration_mod.os.replace

    def spy_replace(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(migration_mod.os, "replace", spy_replace)
    out_dir = tmp_path / "migrations"
    out_dir.mkdir()
    target = out_dir / "skysheep-migration-20260101-000000.zip"
    migration_mod._write_package_file(out_dir, target, b"first")
    migration_mod._write_package_file(out_dir, target, b"second")
    assert len(seen) == 2 and seen[0] != seen[1], "同秒并发导出不得共用同一 tmp 名"
    assert target.read_bytes() == b"second"
    assert not list(out_dir.glob("*.tmp")), "临时文件不留残片"


# ---- F3.5：merge 档覆盖前留备份、失败按备份还原 ----


async def _merge_overwrite_setup(tmp_path: Path) -> tuple[bytes, Path, Path]:
    """A 侧包（技能 demo）+ B 侧本机同名技能（旧 SKILL.md + 本机独有文件）。"""
    home_a = tmp_path / "homeA"
    data = await _build_package(home_a)
    home_b = tmp_path / "homeB"
    home_b.mkdir()
    sk = home_b / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("本机旧版", encoding="utf-8")
    (sk / "本地独有.md").write_text("别删我", encoding="utf-8")
    return data, home_b, sk


async def test_import_merge_backs_up_overwritten_skill(tmp_path):
    """merge 档覆盖前把本机同名技能整份复制留底：成功导入后备份区可回溯。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    data, home_b, sk = await _merge_overwrite_setup(tmp_path)
    result = apply_migration_package(home_b, data, backup_dir=home_b / "backups")
    assert result["skills_overwritten"] == ["demo"]
    # 覆盖照旧：包内 SKILL.md 盖掉同名文件，本机独有文件保留
    assert "迁移测试技能" in (sk / "SKILL.md").read_text(encoding="utf-8")
    assert (sk / "本地独有.md").read_text(encoding="utf-8") == "别删我"
    # 备份区里有覆盖前的本机原目录
    asides = list((home_b / "backups").glob("skills-*/demo"))
    assert len(asides) == 1
    assert (asides[0] / "SKILL.md").read_text(encoding="utf-8") == "本机旧版"
    assert (asides[0] / "本地独有.md").is_file()


def _fail_overlay_copytree(monkeypatch):
    """让「把包内技能拷进本机」这一步失败（备份复制那一跳照常走通）。"""
    import skysheep.server.backend_parts.migration as migration_mod

    real_copytree = migration_mod.shutil.copytree

    def fail_overlay(src, dst, **kw):
        if "skysheep-migration-" in str(src):  # 包内容来自引擎的暂存目录
            raise OSError("模拟覆盖阶段失败")
        return real_copytree(src, dst, **kw)

    monkeypatch.setattr(migration_mod.shutil, "copytree", fail_overlay)


async def test_import_merge_failure_restores_overwritten_skill(tmp_path, monkeypatch):
    """merge 档覆盖后中途失败：本机同名技能按备份原样还原（旧文件 + 独有文件）。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    data, home_b, sk = await _merge_overwrite_setup(tmp_path)
    _fail_overlay_copytree(monkeypatch)
    with pytest.raises(RuntimeError, match="尽力回滚"):
        apply_migration_package(
            home_b, data, backup_dir=home_b / "backups", skills_mode="merge",
        )
    assert (sk / "SKILL.md").read_text(encoding="utf-8") == "本机旧版"
    assert (sk / "本地独有.md").read_text(encoding="utf-8") == "别删我"
    # 备份已搬回原位，备份区不再留 demo
    assert not list((home_b / "backups").glob("skills-*/demo"))


async def test_import_replace_failure_restores_moved_skill(tmp_path, monkeypatch):
    """replace 档既有回滚行为回归：移走的同名技能在失败时原样搬回。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    data, home_b, sk = await _merge_overwrite_setup(tmp_path)
    _fail_overlay_copytree(monkeypatch)
    with pytest.raises(RuntimeError, match="尽力回滚"):
        apply_migration_package(
            home_b, data, backup_dir=home_b / "backups", skills_mode="replace",
        )
    assert (sk / "SKILL.md").read_text(encoding="utf-8") == "本机旧版"
    assert (sk / "本地独有.md").read_text(encoding="utf-8") == "别删我"


async def test_import_failure_names_unrestored_overwritten_skill(tmp_path, monkeypatch):
    """还原也失败时报错逐个点名：残留清单带 skills/<名>，不含糊其辞。"""
    from skysheep.server.backend_parts.migration import apply_migration_package

    data, home_b, _sk = await _merge_overwrite_setup(tmp_path)
    _fail_overlay_copytree(monkeypatch)

    import skysheep.server.backend_parts.migration as migration_mod

    def fail_move(src, dst):
        raise OSError("模拟搬回失败")

    monkeypatch.setattr(migration_mod.shutil, "move", fail_move)
    with pytest.raises(RuntimeError, match="残留未还原：.*skills/demo"):
        apply_migration_package(
            home_b, data, backup_dir=home_b / "backups", skills_mode="merge",
        )
