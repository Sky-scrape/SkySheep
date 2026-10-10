"""安装与升级场景的进程内端到端。

真实安装器（Inno Setup）行为无法在 pytest 里复现，但其用户可见后果可以：
- 全新机器：空 SKYSHEEP_HOME → 引擎各入口（config init / headless / CLI / sessions）
  在零配置下可用，绝不指向真实用户目录（SKYSHEEP_HOME 隔离）。
- 旧版本升级：旧程序写下的库（缺表、缺列、旧 user_version）被新版打开时
  自动补齐、数据保留、pre-migrate 备份落地；再开一次幂等。
- 降级防护：新库被旧版语义（更高 user_version）打开必须拒绝，不静默损坏。

test_store.py 已覆盖迁移的单元面（备份命名、user_version 写入、失败日志）；
本文件从「用户拿着旧数据目录启动新版」的场景视角串全链。
"""

from __future__ import annotations

import sqlite3

import pytest

from skysheep.cli.app import main as cli_main
from skysheep.config import db_path
from skysheep.messages import Message, TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.session.store import SCHEMA_VERSION, SessionStore

# ---- 全新机器：零配置首启 ----


def test_fresh_machine_config_init_creates_template(home, capsys):
    """全新 SKYSHEEP_HOME：skysheep config init 建出模板，重复 init 报错不覆盖。"""
    assert not (home / "home" / "config.toml").exists()
    cli_main(["config", "init"])
    out = capsys.readouterr().out
    assert "created" in out
    assert (home / "home" / "config.toml").exists()
    # 已存在时不覆盖（保护用户填过的 Key）：main() 捕获 ConfigError 打印而非崩溃
    cli_main(["config", "init"])
    assert "already exists" in capsys.readouterr().out


async def test_fresh_machine_headless_first_turn_bootstraps_everything(home):
    """全新 home 直接跑 headless：config 缺失降级、库自动建、消息落库。"""
    from skysheep.cli.app import run_headless

    provider = FakeProvider([[TextBlock(text="全新安装首答")]])
    result = await run_headless(
        "你好", directory=str(home / "proj"),
        provider_factory=lambda: provider, output="text",
    )
    assert result["stop_reason"] == "end_turn"
    store = await SessionStore(db_path()).connect()
    try:
        msgs = await store.load_messages(result["session_id"])
        assert any(m.role == "assistant" and "全新安装首答" in m.text for m in msgs)
        # 全新库直接写最新 schema 版本
        cur = await store._db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
    finally:
        await store.close()


async def test_fresh_machine_sessions_cmd_empty(home, capsys):
    """全新 home：sessions 子命令在空库下友好输出，不崩。"""
    from skysheep.cli.app import _sessions_cmd
    await _sessions_cmd()
    assert "(no sessions)" in capsys.readouterr().out


# ---- 旧版本升级 ----


def _build_legacy_db(path) -> None:
    """手工构造一个「旧版本程序写下的库」：

    - 只有旧程序认识的少数表（projects / sessions / messages / whitelist_rules）；
    - sessions 缺 pinned / archived / memory_digested / tags / summary 列；
    - whitelist_rules 缺 enabled / hit_count / last_hit_at 列；
    - user_version = 0（旧程序没写版本号）；
    - 里面有一条真实对话数据。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            project_id INTEGER REFERENCES projects(id),
            title TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE whitelist_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            tool TEXT NOT NULL,
            kind TEXT NOT NULL,
            pattern TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
    """)
    con.execute("INSERT INTO projects (root_path, name, created_at) VALUES (?, ?, ?)",
                ("C:/old/proj", "旧项目", 1700000000.0))
    con.execute(
        "INSERT INTO sessions (id, project_id, title, created_at, updated_at)"
        " VALUES ('legacy-sess', 1, '旧会话', 1700000000.0, 1700000001.0)")
    msg = Message.user("旧版本里的问题").model_dump_json()
    con.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at)"
        " VALUES ('legacy-sess', 1, 'user', ?, 1700000000.5)", (msg,))
    con.execute(
        "INSERT INTO whitelist_rules (project_id, tool, kind, pattern, created_at)"
        " VALUES (1, 'run_command', 'prefix', 'git status', 1700000000.0)")
    con.commit()
    con.close()


async def test_upgrade_from_legacy_db_preserves_data_and_backs_up(home):
    """旧库被新版打开：自动补列/补表、旧数据完整可读、pre-migrate 备份落地。"""
    db = home / "home" / "skysheep.db"
    _build_legacy_db(db)

    store = await SessionStore(db).connect()
    try:
        # 旧数据逐条可读
        sess = await store.get_session("legacy-sess")
        assert sess is not None and sess.title == "旧会话"
        msgs = await store.load_messages("legacy-sess")
        assert len(msgs) == 1 and "旧版本里的问题" in msgs[0].text
        rules = await store.list_rules(1)
        assert len(rules) == 1 and rules[0]["pattern"] == "git status"
        # 新列已补齐且按默认值工作
        cur = await store._db.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in await cur.fetchall()}
        assert {"pinned", "archived", "memory_digested", "tags", "summary"} <= cols
        # 版本号已升格
        cur = await store._db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
    finally:
        await store.close()

    # pre-migrate 备份存在（升格前的旧库快照）
    backups = list((home / "home" / "backups").glob("pre-migrate-*.db"))
    assert len(backups) == 1
    # 备份的 user_version 仍是 0（升格前的现场；补列在快照前已自动提交，
    # 备份的意义是留底 + 旧程序可按 version=0 库打开，而非结构回退）
    legacy = sqlite3.connect(backups[0])
    try:
        v = legacy.execute("PRAGMA user_version").fetchone()[0]
        assert v == 0
    finally:
        legacy.close()


async def test_upgrade_is_idempotent_on_reopen(home):
    """升级后的库再次打开：不重复备份、数据不变、版本号稳定。"""
    db = home / "home" / "skysheep.db"
    _build_legacy_db(db)

    first = await SessionStore(db).connect()
    try:
        await first.append_message("legacy-sess", Message.assistant([TextBlock(text="新版补写")]))
    finally:
        await first.close()

    backups_before = set((home / "home" / "backups").glob("pre-migrate-*.db"))
    second = await SessionStore(db).connect()
    try:
        msgs = await second.load_messages("legacy-sess")
        assert len(msgs) == 2 and "新版补写" in msgs[1].text
        cur = await second._db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
    finally:
        await second.close()
    backups_after = set((home / "home" / "backups").glob("pre-migrate-*.db"))
    assert backups_before == backups_after, "已升格的库重开不应再产生 pre-migrate 备份"


async def test_downgrade_open_is_rejected(home):
    """新版本库被「更旧程序」打开：明确拒绝而不是静默打开写坏。"""
    db = home / "home" / "skysheep.db"
    store = await SessionStore(db).connect()
    try:
        await store._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    finally:
        await store.close()
    with pytest.raises(RuntimeError, match="降级"):
        await SessionStore(db).connect()


# ---- 升级后数据兼容：新代码在迁移后的库上正常工作 ----


async def test_post_upgrade_store_operations_work(home):
    """迁移后的库跑一遍新代码的常规写路径：追加消息、建会话、开关规则。"""
    db = home / "home" / "skysheep.db"
    _build_legacy_db(db)
    store = await SessionStore(db).connect()
    try:
        new_sess = await store.create_session(1, title="升级后新建")
        await store.append_message(new_sess.id, Message.user("新写入"))
        await store.set_title(new_sess.id, "升级后改名")
        assert (await store.get_session(new_sess.id)).title == "升级后改名"
        # 迁移补出的 enabled 列按默认 1 工作
        rules = await store.list_rules(1)
        assert rules[0]["enabled"] == 1
    finally:
        await store.close()
