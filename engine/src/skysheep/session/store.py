"""SQLite 会话持久化：项目 / 会话 / 消息 / 白名单规则。

消息以归一化 Message 的 JSON 形式存储；加载后可无损恢复 Agent 历史。
注意：查询一律使用占位符参数，不拼接 SQL。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from ..messages import Message

logger = logging.getLogger("skysheep.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    memory_digested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE TABLE IF NOT EXISTS whitelist_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,
    pattern TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    concurrency INTEGER NOT NULL DEFAULT 2,
    created_at REAL NOT NULL,
    finished_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pipeline_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
    seq INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    allowed_tools TEXT NOT NULL DEFAULT '',
    depends_on TEXT NOT NULL DEFAULT '',
    dep_mode TEXT NOT NULL DEFAULT 'all',
    kind TEXT NOT NULL DEFAULT 'run',
    ref_id TEXT NOT NULL DEFAULT '',
    control TEXT NOT NULL DEFAULT '',
    max_runs INTEGER NOT NULL DEFAULT 1,
    timeout_s INTEGER NOT NULL DEFAULT 3600,
    status TEXT NOT NULL DEFAULT 'blocked',
    result TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    runs INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pipeline_nodes_pipeline ON pipeline_nodes(pipeline_id, seq);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
CREATE TABLE IF NOT EXISTS snippets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    sort_order REAL NOT NULL DEFAULT 0,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cron_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL DEFAULT 'interval',
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    time_of_day TEXT NOT NULL DEFAULT '',
    weekday INTEGER NOT NULL DEFAULT -1,
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at REAL NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT '',
    last_result TEXT NOT NULL DEFAULT '',
    next_run_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    start_at REAL NOT NULL,
    end_at REAL NOT NULL DEFAULT 0,
    remind INTEGER NOT NULL DEFAULT 1,
    remind_before INTEGER NOT NULL DEFAULT 0,
    reminded INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_start ON schedules(start_at);
CREATE TABLE IF NOT EXISTS channel_bindings (
    channel TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_sources (
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (channel, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE TABLE IF NOT EXISTS project_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    done_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_project_tasks_project ON project_tasks(project_id, done);
"""

# 全文搜索索引：messages 的 FTS5 虚表（trigram 分词）。
#
# 为什么用 trigram：跨会话搜索要能命中中文子串，而 FTS5 默认的 unicode61
# 分词器把整段中文当一个 token（搜「测试」命不中「中文测试内容」）。trigram
# 按三字符滑窗建索引，中文与英文子串都能直接命中，也不需要外部分词库。
#
# 独立建表（不用 external content 模式）是刻意选择：这样同步只需在写入/删除
# 消息时一并操作本表，不用触发器，也不会因为 messages 的行被 UPDATE 而
# 遗留脏索引。代价只是多存一份文本。
#
# 行号对齐：插入时显式指定 rowid = messages.id，两边一一对应，删除同样按
# rowid 定位；不需要额外的关联列，也不会因自增差异而错位。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    plain,
    tokenize='trigram'
);
"""


@dataclass
class Project:
    id: int
    root_path: str
    name: str
    created_at: float


@dataclass
class Session:
    id: str
    project_id: int | None
    title: str
    created_at: float
    updated_at: float
    pinned: int = 0
    summary: str = ""
    # 用户给会话打的标签（自由文本，逗号分隔存库；侧栏按标签分组）。
    # 用字符串而非单独的表：一个会话标签数很少，分组只需前缀匹配/包含，
    # 不需要为它维护关联表与外键。
    tags: str = ""
    # 归档：1 = 已归档（侧栏默认不显示，可从归档弹窗恢复）
    archived: int = 0


def session_from_row(r) -> Session:
    """从一行 sessions 记录构造 Session（兼容缺列的旧库行）。"""
    return Session(
        r["id"], r["project_id"], r["title"],
        r["created_at"], r["updated_at"], r["pinned"], r["summary"],
        r["tags"] if "tags" in r.keys() else "",
        r["archived"] if "archived" in r.keys() else 0,
    )


def _message_duration_seconds(content_json: str) -> float | None:
    """从消息 JSON 里取实测轮耗时（秒）；没有该字段（旧消息）返回 None。

    直接解 JSON 而不是走 Message.model_validate_json：这里只关心一个标量，
    历史行里可能有旧版本写下的结构，完整校验失败就会丢掉整条样本。
    过滤 1 秒以内（重试/拆分噪音）与 2 小时以上（挂起过夜）与旧逻辑同口径。
    """
    try:
        raw = json.loads(content_json)
    except Exception:  # noqa: BLE001 - 坏行不参与校准
        return None
    if not isinstance(raw, dict):
        return None
    ms = raw.get("duration_ms") or 0
    try:
        sec = int(ms) / 1000.0
    except (TypeError, ValueError):
        return None
    return sec if 1 <= sec <= 7200 else None


def parse_backup_stamp(stamp: str) -> float | None:
    """把备份文件名里的时间戳（`%Y%m%d-%H%M%S`，后面可能跟「-恢复前」）解析成时刻。

    必须按文件名解析、不能拿 mtime 当备份时间：备份是 `shutil.copy2` 复制出来的，
    会连源库的修改时间一起带过来，于是 20 份备份的 mtime 全都一样（都是源库最后
    一次写入的那一刻），列表看起来像一堆重复项。

    解析不了（老文件、手改过名字）返回 None，调用方退回文件 mtime。
    """
    try:
        return time.mktime(time.strptime(stamp[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


# usage_stats 的 project_id 哨兵：None 在项目语义里表示快聊（合法过滤目标），
# 因此「不过滤」用独立哨兵表示，避免与 NULL 项目混淆（安全审查 B14 的过滤参数）。
_ALL = object()


class SessionStore:
    BACKUP_KEEP = 20
    # 恢复前自动留的安全副本后缀（列表里单独标记，方便用户认出「这是恢复动作留下的」）
    SAFETY_TAG = "-恢复前"
    # 启动备份的频率上限：最新一份备份还在这个窗口内就不再重复拷。
    # 桌面应用一天可能启动多次，此前每次启动都全量拷一份——库大了以后启动
    # 变慢（同步 copy2 卡事件循环）、磁盘上还压着 20 份全量。
    BACKUP_MIN_INTERVAL_S = 20 * 3600

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self.backup_created: str | None = None
        # FTS5 全文索引是否就绪（断开后 connect 会重新判定）；
        # False 时 search_messages 退到 LIKE 扫描，功能不降级只是变慢。
        self.fts_ready = False

    async def _rolling_backup(self, existed: bool = True) -> str | None:
        """连接建立后做滚动备份（保留最近 BACKUP_KEEP 份），防止误删无法恢复。

        拷贝与目录清理放线程（同步 copy2 会卡事件循环）；拷之前先
        wal_checkpoint(TRUNCATE) 把 WAL 收进主文件，保证拷到完整的最新数据
        （WAL 模式下已提交的数据可能还在 -wal 里，直接拷 .db 会丢尾巴）。
        频率限制见 BACKUP_MIN_INTERVAL_S：备份时刻按文件名时间戳解析
        （与 list_backups 同口径），窗口内已有备份就只做保留数清理。
        """
        if not existed or not self.path.exists():
            return None
        backup_dir = self.path.parent / "backups"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backups = sorted(backup_dir.glob(self.path.stem + "-*.db"))
            now = time.time()
            fresh = any(
                (s := parse_backup_stamp(f.stem[len(self.path.stem) + 1:])) is not None
                and now - s < self.BACKUP_MIN_INTERVAL_S
                for f in backups
            )
            if fresh:
                self._prune_backups(backups)
                return None
            try:
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001 - checkpoint 失败不拦备份：至多拷到稍旧数据
                pass
            if self.path.stat().st_size <= 0:
                return None
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = backup_dir / f"{self.path.stem}-{stamp}.db"

            def _copy() -> bool:
                try:
                    if not target.exists():
                        shutil.copy2(self.path, target)
                    return True
                except OSError:
                    return False

            if await asyncio.to_thread(_copy):
                self.backup_created = str(target)
            self._prune_backups(sorted(backup_dir.glob(self.path.stem + "-*.db")))
        except OSError:
            return None
        return self.backup_created

    def _prune_backups(self, backups: list[Path]) -> None:
        """只保留最近的 BACKUP_KEEP 份。"""
        for old in backups[: max(0, len(backups) - self.BACKUP_KEEP)]:
            try:
                old.unlink()
            except OSError:
                pass

    # ---- 会话库备份：列出 / 手动备份 / 删除 / 恢复（设置 · 关于） ----

    def backup_dir(self) -> Path:
        return self.path.parent / "backups"

    async def backup_now(self) -> dict:
        """手动触发一次备份（设置 · 关于的「立即备份」按钮）。

        与启动滚动备份的差异：不受 BACKUP_MIN_INTERVAL_S 窗口限制——用户点了
        按钮就是要当前时刻的一份存档；仍按 BACKUP_KEEP 裁剪，总量不会超。
        拷贝放线程、拷之前 checkpoint 收 WAL，与 _rolling_backup 同口径。
        """
        if not self.path.exists() or self.path.stat().st_size <= 0:
            raise RuntimeError("还没有可备份的会话数据")
        d = self.backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        if self._db is not None:
            try:
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001 - checkpoint 失败不拦备份：至多拷到稍旧数据
                pass
        target = d / f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"
        # 同一秒内点第二次：覆盖写，落盘的仍是当前时刻的数据
        await asyncio.to_thread(shutil.copy2, self.path, target)
        self._prune_backups(sorted(d.glob(self.path.stem + "-*.db")))
        return {"name": target.name, "path": str(target)}

    async def delete_backup(self, name: str) -> dict:
        """删除单份备份（设置 · 关于的备份列表，每行一个删除入口）。"""
        d = self.backup_dir()
        # 与 restore_backup 同一套校验：只收纯 .db 文件名，不静默归一化
        if Path(name).name != name or not name.endswith(".db"):
            raise ValueError("备份文件名不合法")
        target = (d / name).resolve()
        try:
            target.relative_to(d.resolve())
        except ValueError:
            raise ValueError("备份文件名不合法") from None
        if not target.is_file():
            raise FileNotFoundError("备份不存在：" + name)
        await asyncio.to_thread(target.unlink)
        return {"deleted": target.name}

    def list_backups(self) -> list[dict]:
        """可用备份列表（新的在前）：备份时刻、大小、路径。

        `taken` 是从文件名时间戳解析出的**备份时刻**，界面按它排序和显示。不要拿 `mtime`
        当备份时间：备份用 `shutil.copy2` 复制，会连源库的修改时间一起带过来，于是所有备份
        都显示成「源库最后一次写入」那一刻，列表看起来像一堆重复项。`safety=True` 的是恢复
        动作自动留下的「恢复前」副本，界面上单独标注。
        """
        d = self.backup_dir()
        if not d.is_dir():
            return []
        items: list[dict] = []
        for f in d.glob(self.path.stem + "-*.db"):
            try:
                st = f.stat()
            except OSError:
                continue
            stamp = f.stem[len(self.path.stem) + 1:]
            items.append({
                "name": f.name,
                "path": str(f),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "taken": parse_backup_stamp(stamp) or st.st_mtime,
                "stamp": stamp,
                "safety": stamp.endswith(self.SAFETY_TAG),
                "current": False,
            })
        try:
            cur = self.path.stat()
            items.append({
                "name": self.path.name,
                "path": str(self.path),
                "size": cur.st_size,
                "mtime": cur.st_mtime,
                "taken": cur.st_mtime,  # 当前库没有备份名字，只能用它自己的修改时间
                "stamp": "当前",
                "safety": False,
                "current": True,
            })
        except OSError:
            pass
        # 当前数据永远排第一；其余按备份时刻（不是 mtime）由新到旧
        items.sort(key=lambda x: (not x["current"], -x["taken"]))
        return items

    async def restore_backup(self, name: str) -> dict:
        """用某个备份覆盖当前会话库（重启前先给现状留一份备份，可再换回来）。

        会关闭再重开数据库连接；调用方（backend）负责随后刷新内存里的会话状态。
        """
        d = self.backup_dir()
        # 只接受纯文件名：带路径分隔符/上级目录的输入直接拒绝，不做静默归一化
        if Path(name).name != name or not name.endswith(".db"):
            raise ValueError("备份文件名不合法")
        src = (d / name).resolve()
        try:
            src.relative_to(d.resolve())
        except ValueError:
            raise ValueError("备份文件名不合法") from None
        if not src.is_file():
            raise FileNotFoundError("备份不存在：" + name)
        # 先给"当前"存一份，用户万一恢复错了还能回来
        safety = None
        if self._db is not None:
            # 与 _rolling_backup / backup_now 同口径：拷贝前先 checkpoint 收 WAL，
            # 否则已提交的数据可能还在 -wal 里，安全副本缺最近一段消息——
            # 恢复失败想退回时退不回「恢复前一刻」
            try:
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001 - checkpoint 失败不拦恢复：至多拷到稍旧数据
                pass
        if self.path.exists() and self.path.stat().st_size > 0:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safety = d / f"{self.path.stem}-{stamp}{self.SAFETY_TAG}.db"
            try:
                shutil.copy2(self.path, safety)
            except OSError:
                safety = None
        if self._db is not None:
            await self._db.close()
            self._db = None
        # 整库拷贝放线程：库到几百 MB 时同步 copy2 会把事件循环冻住数秒
        await asyncio.to_thread(shutil.copy2, src, self.path)
        await self.connect()
        return {
            "restored": src.name,
            "safety_copy": str(safety) if safety else "",
        }

    async def connect(self) -> SessionStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 文件是否本就存在：本次新建的库没有可备的内容，跳过备份（与旧版
        # 「连接前备份」对首启的语义一致）
        existed = self.path.exists()
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # WAL + NORMAL：一轮对话多次小写的 fsync 次数大幅下降，读不再被
        # 写事务的 journal 排他锁挡住；busy_timeout 兜住偶发的写写碰撞。
        # journal_mode 是库级持久属性，synchronous/busy_timeout 是连接级的，
        # 所以每次 connect 都要设全这三样。
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        # 滚动备份要在连接建立、WAL checkpoint 之后做（数据才完整），见 _rolling_backup
        await self._rolling_backup(existed)
        await self._db.executescript(SCHEMA)
        # 旧库迁移：sessions 补 pinned 列（已存在则忽略）
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：schedules 补 remind_before 列（0.5.0 建的表没有；0 = 到点提醒）
        try:
            await self._db.execute(
                "ALTER TABLE schedules ADD COLUMN remind_before INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：schedules 补 end_at 列（可选结束时间；0 = 只记开始时刻，按点事件）
        try:
            await self._db.execute(
                "ALTER TABLE schedules ADD COLUMN end_at REAL NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：sessions 补 summary 列（会话摘要：最近一轮助手回答的开头，侧栏直接看进展）
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        # 旧库迁移：sessions 补 tags 列（用户自定义标签，侧栏可按标签分组）
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN tags TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        # 旧库迁移：sessions 补 archived 列（归档：侧栏默认隐藏，可从归档弹窗恢复）
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：sessions 补 memory_digested 列（归档自动记忆：同会话只提炼一次，
        # 取消归档再归档不重复花钱）
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN memory_digested INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：pipeline_nodes 补 kind / ref_id 列（纳入现有任务：挂接任务簿任务、
        # 会话续跑；本特性发布前建的表没有这两列）
        try:
            await self._db.execute(
                "ALTER TABLE pipeline_nodes ADD COLUMN kind TEXT NOT NULL DEFAULT 'run'"
            )
        except Exception:
            pass
        try:
            await self._db.execute(
                "ALTER TABLE pipeline_nodes ADD COLUMN ref_id TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        # 旧库迁移：补 control / max_runs 列（控制流：条件门 / 终止 / 迭代 + 失败重试）
        try:
            await self._db.execute(
                "ALTER TABLE pipeline_nodes ADD COLUMN control TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        try:
            await self._db.execute(
                "ALTER TABLE pipeline_nodes ADD COLUMN max_runs INTEGER NOT NULL DEFAULT 1"
            )
        except Exception:
            pass
        # 旧库迁移：补 timeout_s 列（节点超时；0 = 不限时）。
        # 存量节点补为 3600（1 小时）：与「无人值守不能无限占用并发槽」的新约定一致
        try:
            await self._db.execute(
                "ALTER TABLE pipeline_nodes ADD COLUMN timeout_s INTEGER NOT NULL DEFAULT 3600"
            )
        except Exception:
            pass
        # 旧库迁移：snippets 补 sort_order / use_count / last_used_at 列（提示词排序
        # 与使用统计）。旧行 sort_order=0，列表先按它升序、同值再按 created_at 倒序，
        # 与升级前「最新创建的在最上」的行为一致。
        try:
            await self._db.execute(
                "ALTER TABLE snippets ADD COLUMN sort_order REAL NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            await self._db.execute(
                "ALTER TABLE snippets ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            await self._db.execute(
                "ALTER TABLE snippets ADD COLUMN last_used_at REAL NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：usage_log 补 cached_tokens 列（提示词缓存命中入账）。旧行补 0
        # （未记录），聚合与费用拆算按 0 处理，行为与升级前一致。
        try:
            await self._db.execute(
                "ALTER TABLE usage_log ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：whitelist_rules 补 enabled / hit_count / last_hit_at 列
        # （规则启停开关与命中统计：临时停用不必删配置，命中情况帮用户清理陈旧规则）
        try:
            await self._db.execute(
                "ALTER TABLE whitelist_rules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        except Exception:
            pass
        try:
            await self._db.execute(
                "ALTER TABLE whitelist_rules ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            await self._db.execute(
                "ALTER TABLE whitelist_rules ADD COLUMN last_hit_at REAL NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        await self._db.commit()
        self._fts_dirty = False  # 索引写失败过 → 下次启动走查漏模式
        await self._setup_fts()
        return self

    # ---- 全文搜索索引（messages_fts） ----

    async def _setup_fts(self) -> None:
        """建 FTS 表并按 rowid 补漏已有消息。失败时置 fts_ready=False，搜索自动退到 LIKE。

        补漏按「messages 里比 FTS 最大 rowid 新的行」增量做：旧库升级后
        第一次启动等于全量回填，日常启动已同步时零成本，历史遗留的漂移
        （旧版本 FTS 写在 commit 之后）也在启动时自动修复。
        虚表建不出来（极少数 SQLite 未编 FTS5）不影响应用启动。
        """
        assert self._db
        self.fts_ready = False
        try:
            await self._db.executescript(FTS_SCHEMA)
        except Exception:
            return  # 没有 FTS5 支持：保留 LIKE 扫描路径
        try:
            await self._backfill_fts()
        except Exception:
            # 回填半途而废会留下水位以下的洞（水位增量只补水位以上的行），
            # 必须置脏：下次启动改走查漏模式，把洞补回来（与 _fts_insert 同口径）
            self._fts_dirty = True
            return
        self.fts_ready = True

    async def _backfill_fts(self) -> None:
        """把 messages 里尚未进 FTS 的行灌进去。

        默认走 rowid 水位增量（旧库首次等于全量，日常启动零成本）；一旦发生过
        索引写失败（_fts_dirty），改用「缺哪行补哪行」的查漏模式——水位法只补
        水位以上的行，修不回水位以下的空洞（安全审查低危项：个别消息搜不到且
        backfill 修不回）。查漏是全表 LEFT JOIN，只在脏标记下走一次。
        """
        assert self._db
        if self._fts_dirty:
            sql = (
                "SELECT m.id AS id, m.content AS content FROM messages m "
                "LEFT JOIN messages_fts f ON f.rowid = m.id WHERE f.rowid IS NULL"
            )
        else:
            sql = (
                "SELECT id, content FROM messages "
                "WHERE id > (SELECT COALESCE(MAX(rowid), 0) FROM messages_fts)"
            )
        cur = await self._db.execute(sql)
        payload = []
        while True:
            rows = await cur.fetchmany(500)
            if not rows:
                break
            payload = [(int(r["id"]), self._plain_text(r["content"])) for r in rows]
            await self._db.executemany(
                "INSERT INTO messages_fts(rowid, plain) VALUES (?, ?)", payload
            )
        if payload:
            await self._db.commit()
            logger.info("FTS 索引补齐 %d 条消息（%s）", len(payload),
                        "查漏模式" if self._fts_dirty else "水位增量")
        self._fts_dirty = False

    @staticmethod
    def _plain_text(raw: str) -> str:
        """消息 JSON → 可搜索的纯文本（与旧 LIKE 搜索的可见内容一致）。"""
        try:
            return Message.model_validate_json(raw).to_plain()
        except Exception:  # noqa: BLE001 - 坏行退化成原始字符串，不能让它挡住索引
            return str(raw)

    async def _fts_insert(self, message_id: int, content_json: str) -> None:
        """同步一条消息进 FTS；未启用时无事发生。

        写失败不影响主存储，但要留痕并置脏：静默吞掉会让这条消息永久搜不到
        （水位法补不回它），脏标记让下次启动走查漏模式修复（安全审查低危项）。
        """
        if not self.fts_ready or self._db is None:
            return
        try:
            await self._db.execute(
                "INSERT INTO messages_fts(rowid, plain) VALUES (?, ?)",
                (int(message_id), self._plain_text(content_json)),
            )
        except Exception as e:  # noqa: BLE001
            self._fts_dirty = True
            logger.warning("FTS 索引写入失败（消息 %s，下次启动补齐）：%s", message_id, e)

    async def _fts_delete(self, where_sql: str, args: tuple) -> None:
        """按 messages 的命中行同步删除 FTS 行（where_sql 作用于 messages 子查询）。"""
        if not self.fts_ready or self._db is None:
            return
        try:
            await self._db.execute(
                "DELETE FROM messages_fts WHERE rowid IN "
                f"(SELECT id FROM messages WHERE {where_sql})",
                args,
            )
        except Exception as e:  # noqa: BLE001
            # 删失败会留下悬挂索引行（搜索 JOIN 会过滤掉，危害有限），
            # 但要留痕并置脏，让下次启动的查漏模式把索引对齐
            self._fts_dirty = True
            logger.warning("FTS 索引删除失败（下次启动对齐）：%s", e)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ---- projects ----

    # 「远程连接」固定项目的哨兵路径：不是真实目录，只作 projects.root_path 的
    # 唯一键（幂等创建/识别都用它）。不指向磁盘上任何位置，永远不会被 switch。
    REMOTE_PROJECT_PATH = "//skysheep-remote"

    async def ensure_remote_project(self) -> Project:
        """取（或建）固定的「远程连接」项目：飞书/微信等渠道的对话都归到它名下。

        渠道对话与桌面上的工作目录无关（工作目录是远程设备的），不落到当前
        项目或快聊里；项目本身不可切换、不可删除（backend/app.py 层拦截）。
        幂等：按哨兵路径唯一键查建，多渠道并发调用也只建一条。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM projects WHERE root_path = ?", (self.REMOTE_PROJECT_PATH,)
        )
        row = await cur.fetchone()
        if row:
            return Project(row["id"], row["root_path"], row["name"], row["created_at"])
        await self._db.execute(
            "INSERT INTO projects (root_path, name, created_at) VALUES (?, ?, ?)",
            (self.REMOTE_PROJECT_PATH, "远程连接", time.time()),
        )
        await self._db.commit()
        cur = await self._db.execute(
            "SELECT * FROM projects WHERE root_path = ?", (self.REMOTE_PROJECT_PATH,)
        )
        row = await cur.fetchone()
        return Project(row["id"], row["root_path"], row["name"], row["created_at"])

    async def get_or_create_project(self, root_path: str, name: str = "") -> Project:
        assert self._db
        root_path = str(Path(root_path).resolve())
        cur = await self._db.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        )
        row = await cur.fetchone()
        if row:
            return Project(row["id"], row["root_path"], row["name"], row["created_at"])
        cur = await self._db.execute(
            "INSERT INTO projects (root_path, name, created_at) VALUES (?, ?, ?)",
            (root_path, name or Path(root_path).name, time.time()),
        )
        await self._db.commit()
        return Project(cur.lastrowid, root_path, name or Path(root_path).name, time.time())

    async def list_projects(self) -> list[Project]:
        assert self._db
        # created_at 是秒级，同一秒内建的项目顺序不稳定；id 自增作次级键，
        # 同秒时后建的 id 更大排前面，与「新的在前」语义一致
        cur = await self._db.execute(
            "SELECT * FROM projects ORDER BY created_at DESC, id DESC")
        rows = await cur.fetchall()
        return [Project(r["id"], r["root_path"], r["name"], r["created_at"]) for r in rows]

    async def get_project(self, project_id: int) -> Project | None:
        """按 id 取项目记录（定时任务按自身项目执行时解析工作目录用）。"""
        assert self._db
        cur = await self._db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cur.fetchone()
        return Project(row["id"], row["root_path"], row["name"], row["created_at"]) if row else None

    async def delete_project(self, project_id: int) -> int:
        """删除项目记录及其全部会话（含消息）、白名单规则、任务清单与定时任务/
        流水线（与 backend.delete_project 的声明同一口径），返回删除的行数（0=不存在）。
        只清数据库记录，电脑上的项目文件夹不受影响。

        定时任务/流水线必须级联：留下孤儿任务的话，到点扫描仍会捞到它，
        执行上下文兜底会把它挂到「当前项目」的工作目录与白名单下继续跑。"""
        assert self._db
        await self._fts_delete(
            "session_id IN (SELECT id FROM sessions WHERE project_id = ?)", (project_id,)
        )
        await self._db.execute(
            "DELETE FROM messages WHERE session_id IN "
            "(SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        await self._db.execute("DELETE FROM sessions WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM whitelist_rules WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM project_tasks WHERE project_id = ?", (project_id,))
        await self._db.execute(
            "DELETE FROM pipeline_nodes WHERE pipeline_id IN "
            "(SELECT id FROM pipelines WHERE project_id = ?)",
            (project_id,),
        )
        await self._db.execute("DELETE FROM pipelines WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM cron_tasks WHERE project_id = ?", (project_id,))
        cur = await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self._db.commit()
        return cur.rowcount

    # ---- 项目任务清单 ----

    @staticmethod
    def _project_task_row(row) -> dict:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "detail": row["detail"],
            "done": bool(row["done"]),
            "created_at": row["created_at"],
            "done_at": row["done_at"],
        }

    async def add_project_task(self, project_id: int, title: str, detail: str = "") -> dict:
        assert self._db
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO project_tasks (project_id, title, detail, created_at) VALUES (?, ?, ?, ?)",
            (project_id, title, detail, now),
        )
        await self._db.commit()
        row = await self.get_project_task(cur.lastrowid)  # type: ignore[arg-type]
        assert row is not None
        return row

    async def get_project_task(self, task_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM project_tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return self._project_task_row(row) if row else None

    async def list_project_tasks(self, project_id: int) -> list[dict]:
        """未完成在前（按加入顺序，像清单步骤），已完成垫底（按完成时间倒序）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM project_tasks WHERE project_id = ? "
            "ORDER BY done ASC, CASE WHEN done = 0 THEN id ELSE -done_at END",
            (project_id,),
        )
        rows = await cur.fetchall()
        return [self._project_task_row(r) for r in rows]

    async def update_project_task(
        self, task_id: int, *, title: str | None = None, detail: str | None = None,
        done: bool | None = None,
    ) -> dict | None:
        """按传入字段部分更新；勾掉/重开时刷新 done_at。"""
        assert self._db
        row = await self.get_project_task(task_id)
        if not row:
            return None
        sets: list[str] = []
        args: list = []
        if title is not None:
            sets.append("title = ?")
            args.append(title)
        if detail is not None:
            sets.append("detail = ?")
            args.append(detail)
        if done is not None:
            sets.append("done = ?")
            args.append(1 if done else 0)
            sets.append("done_at = ?")
            args.append(time.time() if done else 0)
        if sets:
            args.append(task_id)
            await self._db.execute(
                f"UPDATE project_tasks SET {', '.join(sets)} WHERE id = ?", args
            )
            await self._db.commit()
        return await self.get_project_task(task_id)

    async def delete_project_task(self, task_id: int) -> int:
        assert self._db
        cur = await self._db.execute("DELETE FROM project_tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return cur.rowcount

    # ---- sessions ----

    async def create_session(self, project_id: int | None, title: str = "") -> Session:
        assert self._db
        now = time.time()
        sid = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO sessions (id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (sid, project_id, title, now, now),
        )
        await self._db.commit()
        return Session(sid, project_id, title, now, now)

    async def get_session(self, session_id: str) -> Session | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return session_from_row(row)

    async def get_session_for_project(
        self, session_id: str, project_id: int | None
    ) -> Session | None:
        """按 id 取会话并校验项目归属：不属于该项目的（含快聊边界）返回 None。

        安全边界：get_session 只按 id 查询，持有远程令牌的客户端可以拿别的
        项目的 session_id 走 chat.send / export / delete 等接口，把他人会话
        挂进当前项目的工作目录与权限门下执行。所有跨网络的按 id 操作必须
        走本方法；project_id=None 表示快聊（project_id IS NULL 的会话）。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE id = ?"
            " AND ((? IS NULL AND project_id IS NULL) OR project_id = ?)",
            (session_id, project_id, project_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return session_from_row(row)

    async def list_sessions(self, project_id: int | None = None, limit: int = 50) -> list[Session]:
        """侧栏会话列表（已归档的不在其中，见 list_archived_sessions）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT ?", (limit,)
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (project_id, limit),
            )
        rows = await cur.fetchall()
        return [session_from_row(r) for r in rows]

    async def list_quick_sessions(self, limit: int = 50) -> list[Session]:
        """只取快聊会话（project_id IS NULL），最近活跃在前。

        经典视图的「快聊」区块用它：list_sessions(None) 的语义是「全部会话」
        （CLI 与渠道在用），拿它当快聊会连别的项目的会话一起捞出来，所以
        单独一条口径。已归档的同样不在这里（去归档弹窗恢复）。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE project_id IS NULL AND archived = 0"
            " ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [session_from_row(r) for r in rows]

    async def list_sessions_by_project(
        self, limit_per_project: int = 50
    ) -> dict[int | None, list[Session]]:
        """分组侧栏用：每个项目各自取最近未归档会话（置顶在前）。

        一个窗口函数按 project_id 分区各取前 N 条，避免「全局前 N」把会话多的
        项目挤没；project_id 为 NULL 的快聊会话自成一区（键为 None）。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM ("
            "  SELECT s.*, ROW_NUMBER() OVER ("
            "    PARTITION BY project_id ORDER BY pinned DESC, updated_at DESC) AS _rn"
            "  FROM sessions s WHERE s.archived = 0"
            ") WHERE _rn <= ? ORDER BY project_id, pinned DESC, updated_at DESC",
            (limit_per_project,),
        )
        rows = await cur.fetchall()
        out: dict[int | None, list[Session]] = {}
        for r in rows:
            s = session_from_row(r)
            out.setdefault(s.project_id, []).append(s)
        return out

    async def list_archived_sessions(
        self, project_id: int | None = None, limit: int = 100,
        include_projectless: bool = False,
    ) -> list[Session]:
        """已归档会话（归档弹窗用），最近活跃在前。

        include_projectless=True 时连同快聊（project_id IS NULL）的归档会话
        一起返回：侧栏的「快聊」分组是常驻的，它的会话归档后必须能在同一个
        弹窗里找回，否则不属于任何项目就意味着归档即永久消失。
        """
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE archived = 1"
                " ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        elif include_projectless:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE archived = 1"
                " AND (project_id = ? OR project_id IS NULL)"
                " ORDER BY updated_at DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND archived = 1"
                " ORDER BY updated_at DESC LIMIT ?",
                (project_id, limit),
            )
        rows = await cur.fetchall()
        return [session_from_row(r) for r in rows]

    async def count_archived_sessions(
        self, project_id: int | None = None, include_projectless: bool = False
    ) -> int:
        """当前项目（或全部项目）的归档会话数，侧栏归档入口的角标用。

        口径与 list_archived_sessions 一致（include_projectless 时含快聊），
        否则角标会漏报快聊的归档会话，入口根本不出现。
        """
        assert self._db
        if project_id is None:
            cur = await self._db.execute("SELECT COUNT(*) AS n FROM sessions WHERE archived = 1")
        elif include_projectless:
            cur = await self._db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE archived = 1"
                " AND (project_id = ? OR project_id IS NULL)",
                (project_id,),
            )
        else:
            cur = await self._db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE project_id = ? AND archived = 1",
                (project_id,),
            )
        row = await cur.fetchone()
        return int(row["n"] or 0) if row else 0

    async def set_archived(self, session_id: str, archived: bool) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET archived = ? WHERE id = ?", (1 if archived else 0, session_id)
        )
        await self._db.commit()

    async def get_session_memory_digested(self, session_id: str) -> bool:
        """归档自动记忆是否已对该会话提炼过（取消归档再归档不重复提炼）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT memory_digested FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return bool(row and row[0])

    async def mark_session_memory_digested(self, session_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET memory_digested = 1 WHERE id = ?", (session_id,)
        )
        await self._db.commit()

    async def find_last_user_seq(self, session_id: str) -> int | None:
        """最后一条 user 消息的 seq（消息级重生成/编辑的默认锚点）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT seq FROM messages WHERE session_id = ? AND role = 'user'"
            " ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
        return row["seq"] if row else None

    async def search_messages_by_seq(self, session_id: str, before_seq: int, role: str) -> int | None:
        """锚点之前（seq 更小）最近一条指定角色的消息 seq。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT seq FROM messages WHERE session_id = ? AND role = ? AND seq < ?"
            " ORDER BY seq DESC LIMIT 1",
            (session_id, role, before_seq),
        )
        row = await cur.fetchone()
        return row["seq"] if row else None

    async def max_seq(self, session_id: str) -> int | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT MAX(seq) AS m FROM messages WHERE session_id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return row["m"] if row and row["m"] is not None else None

    async def get_message_at(self, session_id: str, seq: int) -> dict | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT seq, role, content FROM messages WHERE session_id = ? AND seq = ?",
            (session_id, seq),
        )
        row = await cur.fetchone()
        if not row:
            return None
        try:
            plain = Message.model_validate_json(row["content"]).to_plain()
        except Exception:
            plain = ""
        return {"seq": row["seq"], "role": row["role"], "text": plain}

    async def truncate_from(self, session_id: str, seq: int, include_self: bool) -> int:
        """删除 seq 在锚点之后（或含锚点）的消息，返回删除条数。

        锚点之后可能残留比锚点大的 seq，用 > / >= 锚点即可（seq 单调递增）。
        """
        assert self._db
        op = ">=" if include_self else ">"
        # FTS 是独立表，删 messages 前先按同一条件清掉对应行
        await self._fts_delete(f"session_id = ? AND seq {op} ?", (session_id, seq))
        cur = await self._db.execute(
            f"DELETE FROM messages WHERE session_id = ? AND seq {op} ?",
            (session_id, seq),
        )
        await self._db.commit()
        return cur.rowcount

    async def copy_messages_between(
        self, src_session: str, dst_session: str, upto_seq: int
    ) -> int:
        """把 seq <= upto_seq 的消息复制到新会话（分叉用），返回复制条数。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT seq, role, content, created_at FROM messages"
            " WHERE session_id = ? AND seq <= ? ORDER BY seq ASC",
            (src_session, upto_seq),
        )
        rows = await cur.fetchall()
        n = 0
        for r in rows:
            cur2 = await self._db.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (dst_session, r["seq"], r["role"], r["content"], r["created_at"]),
            )
            if cur2.lastrowid is not None:
                await self._fts_insert(int(cur2.lastrowid), r["content"])
            n += 1
        await self._db.commit()
        return n

    async def get_session_title(self, session_id: str) -> str:
        """轻量取标题（多会话管线收尾用，不拉全部列）。"""
        assert self._db
        cur = await self._db.execute("SELECT title FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return (row["title"] if row else "") or ""

    async def set_summary(self, session_id: str, summary: str) -> None:
        """更新会话摘要（最近一轮助手回答的开头，侧栏展示用）。"""
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET summary = ? WHERE id = ?", (summary, session_id)
        )
        await self._db.commit()

    async def set_pinned(self, session_id: str, pinned: bool) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET pinned = ? WHERE id = ?", (1 if pinned else 0, session_id)
        )
        await self._db.commit()

    async def set_tags(self, session_id: str, tags: list[str] | str) -> str:
        """设置会话标签，返回归一化后的存库字符串。

        归一化规则：去除空白与逗号（标签用逗号分隔存一列）、去重、保持顺序，
        总数限 12 个、每个不超 24 字——侧栏展示位置有限，不靠数据库拦截奇葩输入。
        """
        assert self._db
        items: list[str] = []
        raw = tags if isinstance(tags, str) else ",".join(str(t) for t in tags)
        for piece in str(raw).replace("，", ",").split(","):
            t = piece.strip()[:24]
            if t and t not in items:
                items.append(t)
            if len(items) >= 12:
                break
        value = ",".join(items)
        await self._db.execute(
            "UPDATE sessions SET tags = ? WHERE id = ?", (value, session_id)
        )
        await self._db.commit()
        return value

    async def list_all_tags(self, project_id: int | None) -> list[dict]:
        """当前项目下出现过的标签及各自会话数（侧栏分组头部用）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT tags FROM sessions WHERE project_id IS NULL AND tags != ''"
            )
        else:
            cur = await self._db.execute(
                "SELECT tags FROM sessions WHERE project_id = ? AND tags != ''", (project_id,)
            )
        rows = await cur.fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            for t in str(r["tags"] or "").split(","):
                t = t.strip()
                if t:
                    counts[t] = counts.get(t, 0) + 1
        return [
            {"tag": t, "count": n}
            for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    async def latest_session(self, project_id: int | None) -> Session | None:
        """取项目下最近活跃的会话（用于启动时"接着上次继续"；已归档不参与）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id IS NULL AND archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT 1"
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT 1",
                (project_id,),
            )
        row = await cur.fetchone()
        if not row:
            return None
        return session_from_row(row)

    async def count_empty_sessions(self, project_id: int | None) -> int:
        """统计能被 delete_empty_sessions 清掉的空会话数（"清理 N 个空会话"角标）。

        口径必须与 delete_empty_sessions 完全一致（置顶/归档的不算）：
        角标显示 N、点清理却删不掉 N 个，操作看起来就像坏了。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id IS NULL"
                " AND pinned = 0 AND archived = 0"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages)"
            )
        else:
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?"
                " AND pinned = 0 AND archived = 0"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages)",
                (project_id,),
            )
        row = await cur.fetchone()
        return int(row[0])

    async def delete_empty_sessions(self, project_id: int | None, keep_id: str | None = None) -> int:
        """删除空会话（保留 keep_id 指定的当前会话与置顶会话），返回删除数量。

        归档的空会话不动：用户特意归档收起来的东西，不该被清理顺手删掉。"""
        assert self._db
        keep = keep_id or ""
        if project_id is None:
            cur = await self._db.execute(
                "DELETE FROM sessions WHERE project_id IS NULL AND pinned = 0"
                " AND archived = 0"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages) AND id != ?",
                (keep,),
            )
        else:
            cur = await self._db.execute(
                "DELETE FROM sessions WHERE project_id = ? AND pinned = 0"
                " AND archived = 0"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages) AND id != ?",
                (project_id, keep),
            )
        await self._db.commit()
        return cur.rowcount or 0

    async def move_session(self, session_id: str, project_id: int | None) -> None:
        """把会话移动到另一个项目（project_id 为 NULL 表示移入快聊）。"""
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET project_id = ? WHERE id = ?", (project_id, session_id)
        )
        await self._db.commit()

    async def set_title(self, session_id: str, title: str) -> None:
        assert self._db
        await self._db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        await self._db.commit()

    async def touch(self, session_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
        )
        await self._db.commit()

    async def delete_session(self, session_id: str) -> None:
        """删除会话及其全部消息。"""
        assert self._db
        await self._fts_delete("session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()

    # ---- messages ----

    async def append_message(self, session_id: str, message: Message) -> int:
        assert self._db
        cur = await self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        seq = row[0]
        content_json = message.model_dump_json()
        cur = await self._db.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, message.role, content_json, time.time()),
        )
        # FTS 与主表同一事务：写在 commit 之后会挂进新的隐式事务，
        # 靠下一次写操作的 commit 才顺带提交，收尾/崩溃时尾部落空
        await self._fts_insert(int(cur.lastrowid), content_json)
        await self._db.commit()
        message.seq = seq  # 回填给内存历史/前端 brief 用
        return cur.lastrowid

    async def load_messages(self, session_id: str) -> list[Message]:
        assert self._db
        cur = await self._db.execute(
            "SELECT seq, content FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            m = Message.model_validate_json(r["content"])
            m.seq = r["seq"]  # 回填真实序号（1 基；截断后仍单调用于锚点比较）
            out.append(m)
        return out

    async def search_messages(
        self, project_id: int | None, query: str, limit: int = 20, scope: str = "project"
    ) -> list[dict]:
        """跨会话搜索消息内容。

        优先走 FTS5（trigram 分词）——中文子串、大小写不敏感、不扫全表；
        FTS 不可用时（旧库无虚表 / SQLite 未编 FTS5 / 查询语法异常）自动退到
        LIKE 扫描，行为与之前一致。

        每个会话只取最新排序下最先命中的片段，返回 [{session_id,title,snippet,…}]。
        scope="all" 时忽略项目过滤，在所有项目（含快聊）里找——用户在多个项目之间
        往往记不住某件事是在哪个项目里聊的。结果附带项目名，便于在列表里区分。
        """
        assert self._db
        query = (query or "").strip()
        if not query:
            return []
        if self.fts_ready:
            rows = await self._search_fts(project_id, query, scope)
        else:
            rows = await self._search_like(project_id, query, scope)

        results: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            if r["session_id"] in seen:
                continue
            seen.add(r["session_id"])
            plain = self._plain_text(r["content"])
            # 命中位置用大小写不敏感查找（FTS 与 LIKE 都可能在大小写上放宽）
            low, q = plain.lower(), query.lower()
            pos = low.find(q)
            if pos < 0:
                pos = 0  # 行确实命中（SQL 侧放宽）但正文里定位不到：片段从头截
            start = max(0, pos - 40)
            fragment = plain[start : pos + len(query) + 80].replace("\n", " ").strip()
            results.append(
                {
                    "session_id": r["session_id"],
                    "title": r["title"],
                    "snippet": ("…" if start else "") + fragment + "…",
                    "updated_at": r["updated_at"],
                    "project_id": r["project_id"],
                    "project_name": r["project_name"] or "快聊",
                    "project_path": r["project_path"] or "",
                }
            )
            if len(results) >= limit:
                break
        return results

    SEARCH_SELECT = (
        "SELECT m.session_id, s.title, s.updated_at, m.content, s.project_id,"
        "       p.name AS project_name, p.root_path AS project_path"
        " FROM messages m"
        " JOIN sessions s ON s.id = m.session_id"
        " LEFT JOIN projects p ON p.id = s.project_id"
    )

    def _scope_condition(self, project_id: int | None, scope: str) -> tuple[str, tuple]:
        # 已归档会话不进搜索结果（归档 = 眼不见；要找就去归档弹窗恢复）
        if scope == "all":
            return "s.archived = 0", ()
        cond = "s.project_id IS NULL" if project_id is None else "s.project_id = ?"
        args = (project_id,) if project_id is not None else ()
        return f"({cond}) AND s.archived = 0", args

    async def _search_fts(self, project_id: int | None, query: str, scope: str) -> list:
        """FTS5 查询；语法异常或虚表缺失时自动退到 LIKE。

        trigram 索引要求查询至少 3 个字符才有意义（少于 3 字符无法构成一个
        三字符片段），这种短查询直接走 LIKE——结果更准，也不浪费一次抛错。
        """
        if len(query) < 3:
            return await self._search_like(project_id, query, scope)
        cond, args = self._scope_condition(project_id, scope)
        try:
            # 用双引号包成字符串字面量：不加引号时查询里的 " - * : ^ 等
            # 会被 FTS 当成语法，普通用户的搜索词不该有这种副作用。
            match = '"' + query.replace('"', '""') + '"'
            cur = await self._db.execute(
                self.SEARCH_SELECT
                + " JOIN messages_fts f ON f.rowid = m.id"
                + f" WHERE {cond} AND messages_fts MATCH ?"
                " ORDER BY s.pinned DESC, s.updated_at DESC, m.seq",
                args + (match,),
            )
            return await cur.fetchall()
        except Exception:  # noqa: BLE001 - 索引/语法问题不应对用户显形
            return await self._search_like(project_id, query, scope)

    async def _search_like(self, project_id: int | None, query: str, scope: str) -> list:
        """LIKE 全扫描：FTS 不可用时的兜底（也用于 1–2 字符的短查询）。

        通配符要转义：查询里的 % / _ 是用户想找的字面字符，不当通配符——
        不转义的话搜「%」等于全表命中，搜「100%」变成前缀匹配。"""
        cond, args = self._scope_condition(project_id, scope)
        escaped = (
            query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        cur = await self._db.execute(
            self.SEARCH_SELECT
            + f" WHERE {cond} AND m.content LIKE ? ESCAPE '\\'"
            " ORDER BY s.pinned DESC, s.updated_at DESC, m.seq",
            args + (f"%{escaped}%",),
        )
        return await cur.fetchall()

    # ---- 任务耗时预估的实测样本 ----

    async def recent_turn_seconds(self, project_id: int | None, limit: int = 30) -> list[float]:
        """本项目最近 N 次「用户消息→助手回复」的实测耗时（秒），新→旧。

        优先读消息自带的 duration_ms（轮末实测墙钟，含工具与思考时间）——
        一轮的 user/assistant 消息是轮末批量落库的，两条 created_at 只差几毫秒，
        按时间差反推轮耗时从来算不准（旧写法因此一直拿不到样本）。旧库里的
        历史消息没有该字段，仍按 created_at 差值兜底（剔除 1 秒以内与 2 小时
        以上：前者是重试/拆分噪音，后者是挂起过夜或中途离开）。
        供 core/estimate.py 做历史校准——启发式再准也不如用户机器上的真实记录。
        """
        assert self._db
        cond = "s.project_id IS NULL" if project_id is None else "s.project_id = ?"
        args = () if project_id is None else (project_id,)
        cur = await self._db.execute(
            "SELECT a.content AS acontent, a.created_at - u.created_at AS dur"
            " FROM messages u"
            " JOIN sessions s ON s.id = u.session_id"
            " JOIN messages a"
            "   ON a.session_id = u.session_id AND a.seq = u.seq + 1 AND a.role = 'assistant'"
            f" WHERE u.role = 'user' AND {cond}"
            " ORDER BY u.created_at DESC"
            " LIMIT ?",
            args + (limit * 2,),
        )
        rows = await cur.fetchall()
        out: list[float] = []
        for r in rows:
            real = _message_duration_seconds(r["acontent"])
            if real is not None:
                out.append(real)
            elif 1 <= r["dur"] <= 7200:
                out.append(r["dur"])
            if len(out) >= limit:
                break
        return out

    # ---- whitelist rules ----

    async def add_rule(self, project_id: int, tool: str, kind: str, pattern: str = "") -> None:
        assert self._db
        await self._db.execute(
            "INSERT INTO whitelist_rules"
            " (project_id, tool, kind, pattern, created_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, tool, kind, pattern, time.time()),
        )
        await self._db.commit()

    async def list_rules(self, project_id: int) -> list[dict]:
        assert self._db
        # 新规则在前（id 倒序）：设置页白名单列表与最近一次「总是允许」的操作对得上
        cur = await self._db.execute(
            "SELECT id, tool, kind, pattern, created_at, enabled, hit_count, last_hit_at"
            " FROM whitelist_rules WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "tool": r["tool"],
                "kind": r["kind"],
                "pattern": r["pattern"],
                "created_at": r["created_at"],
                "enabled": bool(r["enabled"]),
                "hit_count": r["hit_count"],
                "last_hit_at": r["last_hit_at"],
            }
            for r in rows
        ]

    async def set_rule_enabled(
        self, rule_id: int, project_id: int, enabled: bool,
    ) -> None:
        """启停一条白名单规则。归属条件写进 UPDATE：凭枚举到的 rule_id
        不能改其他项目的规则（与 remove_rule 同一约束）。"""
        assert self._db
        cur = await self._db.execute(
            "UPDATE whitelist_rules SET enabled = ? WHERE id = ? AND project_id = ?",
            (1 if enabled else 0, rule_id, project_id),
        )
        await self._db.commit()
        if not cur.rowcount:
            raise KeyError("rule not found in this project")

    async def record_rule_hit(self, rule_id: int) -> None:
        """白名单命中记账：次数 +1、刷新最近命中时间。

        authorize 的放行路径上调用，只做一次小 UPDATE；失败由调用方吞掉
        （记账不该影响放行）。
        """
        assert self._db
        await self._db.execute(
            "UPDATE whitelist_rules SET hit_count = hit_count + 1, last_hit_at = ?"
            " WHERE id = ?",
            (time.time(), rule_id),
        )
        await self._db.commit()

    async def clear_rules(self, project_id: int, kind: str = "") -> int:
        """清空项目的白名单规则（可按 kind 过滤），返回删除条数。"""
        assert self._db
        if kind:
            cur = await self._db.execute(
                "DELETE FROM whitelist_rules WHERE project_id = ? AND kind = ?",
                (project_id, kind),
            )
        else:
            cur = await self._db.execute(
                "DELETE FROM whitelist_rules WHERE project_id = ?", (project_id,)
            )
        await self._db.commit()
        return cur.rowcount or 0

    async def remove_rule(self, rule_id: int, project_id: int | None = None) -> None:
        """删除一条白名单规则；给出 project_id 时只删该项目的规则。

        远程客户端不该能凭枚举到的 rule_id 删掉别的项目的白名单
        （安全审查 B11），store 层直接把归属条件写进 DELETE。
        """
        assert self._db
        if project_id is None:
            await self._db.execute("DELETE FROM whitelist_rules WHERE id = ?", (rule_id,))
        else:
            await self._db.execute(
                "DELETE FROM whitelist_rules WHERE id = ? AND project_id = ?",
                (rule_id, project_id),
            )
        await self._db.commit()

    # ---- 聊天软件渠道（Bot Channel）：平台与会话的绑定 + 见过的来源 ----

    async def get_channel_binding(self, channel: str) -> str | None:
        """取某个渠道当前绑定的会话 id（没有则 None）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT session_id FROM channel_bindings WHERE channel = ?", (channel,)
        )
        row = await cur.fetchone()
        return row["session_id"] if row else None

    async def set_channel_binding(self, channel: str, session_id: str) -> None:
        """绑定（或改绑）渠道到会话。UPSERT：同一平台重复绑定只保留最新一条。"""
        assert self._db
        await self._db.execute(
            "INSERT INTO channel_bindings (channel, session_id, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(channel) DO UPDATE SET session_id = excluded.session_id,"
            " updated_at = excluded.updated_at",
            (channel, session_id, time.time()),
        )
        await self._db.commit()

    async def find_channel_by_session(self, session_id: str) -> str | None:
        """反查：这个会话绑定的是哪个渠道。

        自愈路径需要它：runtime 丢了之后要重建，重建时要拿到平台名去取渠道配置，
        而平台名的事实来源是这个绑定关系。
        """
        assert self._db
        cur = await self._db.execute(
            "SELECT channel FROM channel_bindings WHERE session_id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return row["channel"] if row else None

    async def clear_channel_binding(self, channel: str) -> None:
        assert self._db
        await self._db.execute("DELETE FROM channel_bindings WHERE channel = ?", (channel,))
        await self._db.commit()

    async def record_channel_source(self, channel: str, chat_id: str, actor: str = "") -> None:
        """记一个「见过的来源」，供桌面端认领 chat_id。

        只在名单外调用。同一 (channel, chat_id) 重复出现只累加 hits 与 last_seen，
        否则重试风暴会把表写成垃圾。
        """
        assert self._db
        now = time.time()
        await self._db.execute(
            "INSERT INTO channel_sources (channel, chat_id, actor, first_seen, last_seen, hits)"
            " VALUES (?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(channel, chat_id) DO UPDATE SET"
            " actor = excluded.actor, last_seen = excluded.last_seen, hits = hits + 1",
            (channel, chat_id, actor, now, now),
        )
        await self._db.commit()

    async def list_channel_sources(self, channel: str | None = None) -> list[dict]:
        """列出见过的来源，最近出现的在前。"""
        assert self._db
        if channel:
            cur = await self._db.execute(
                "SELECT * FROM channel_sources WHERE channel = ? ORDER BY last_seen DESC",
                (channel,),
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM channel_sources ORDER BY last_seen DESC"
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---- schedules（用户级日程，存在全局库里，切项目不丢） ----

    @staticmethod
    def _schedule_row(r) -> dict:
        return {
            "id": r["id"],
            "title": r["title"],
            "notes": r["notes"],
            "start_at": r["start_at"],
            # 0 = 未设结束时间（按点事件）；>0 时 start_at~end_at 是一个时间段
            "end_at": float(r["end_at"] or 0),
            "remind": bool(r["remind"]),
            "remind_before": int(r["remind_before"]),
            "reminded": bool(r["reminded"]),
            "done": bool(r["done"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    async def add_schedule(
        self,
        title: str,
        start_at: float,
        notes: str = "",
        remind: bool = True,
        remind_before: int = 0,
        end_at: float = 0,
    ) -> dict:
        assert self._db
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO schedules (title, notes, start_at, end_at, remind, remind_before,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                notes,
                start_at,
                max(0.0, float(end_at or 0)),
                1 if remind else 0,
                max(0, int(remind_before)),
                now,
                now,
            ),
        )
        await self._db.commit()
        return await self.get_schedule(cur.lastrowid)  # type: ignore[return-value]

    async def get_schedule(self, schedule_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        row = await cur.fetchone()
        return self._schedule_row(row) if row else None

    async def list_schedules(self, include_done: bool = False, limit: int = 500) -> list[dict]:
        assert self._db
        sql = "SELECT * FROM schedules"
        if not include_done:
            sql += " WHERE done = 0"
        sql += " ORDER BY start_at LIMIT ?"
        cur = await self._db.execute(sql, (limit,))
        rows = await cur.fetchall()
        return [self._schedule_row(r) for r in rows]

    async def update_schedule(
        self,
        schedule_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
        remind: bool | None = None,
        remind_before: int | None = None,
        done: bool | None = None,
    ) -> dict | None:
        """按传入字段部分更新；start_at 变化时重置已提醒标记（新时间要重新提醒）。

        end_at 传 0（或 None 以外的零值）即清除结束时间，回到按点事件。
        """
        assert self._db
        row = await self.get_schedule(schedule_id)
        if not row:
            return None
        sets: list[str] = []
        args: list = []
        if title is not None:
            sets.append("title = ?")
            args.append(title)
        if notes is not None:
            sets.append("notes = ?")
            args.append(notes)
        if start_at is not None:
            sets.append("start_at = ?")
            args.append(start_at)
            sets.append("reminded = 0")
        if end_at is not None:
            sets.append("end_at = ?")
            args.append(max(0.0, float(end_at or 0)))
        if remind is not None:
            sets.append("remind = ?")
            args.append(1 if remind else 0)
        if remind_before is not None:
            sets.append("remind_before = ?")
            args.append(max(0, int(remind_before)))
        if done is not None:
            sets.append("done = ?")
            args.append(1 if done else 0)
        if not sets:
            return row
        sets.append("updated_at = ?")
        args.extend([time.time(), schedule_id])
        await self._db.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", args)
        await self._db.commit()
        return await self.get_schedule(schedule_id)

    async def delete_schedule(self, schedule_id: int) -> bool:
        assert self._db
        cur = await self._db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await self._db.commit()
        return bool(cur.rowcount)

    async def due_schedules(self, now: float | None = None) -> list[dict]:
        """到点未提醒的日程（含提前量：start_at - remind_before 分钟 <= now）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM schedules WHERE remind = 1 AND reminded = 0 AND done = 0"
            " AND (start_at - remind_before * 60) <= ? ORDER BY start_at",
            (now if now is not None else time.time(),),
        )
        rows = await cur.fetchall()
        return [self._schedule_row(r) for r in rows]

    async def mark_schedule_reminded(self, schedule_id: int) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE schedules SET reminded = 1, updated_at = ? WHERE id = ?",
            (time.time(), schedule_id),
        )
        await self._db.commit()

    # ---- 用量统计（usage_log）：每轮对话的 token 消耗 ----

    async def add_usage(
        self, session_id: str, provider: str, model: str,
        in_tokens: int, out_tokens: int, cached_tokens: int = 0,
    ) -> None:
        if in_tokens <= 0 and out_tokens <= 0:
            return
        assert self._db
        await self._db.execute(
            "INSERT INTO usage_log (session_id, provider, model, ts, in_tokens, out_tokens, cached_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, provider, model, time.time(), in_tokens, out_tokens, max(0, cached_tokens)),
        )
        await self._db.commit()

    async def usage_stats(self, days: int = 14, *, project_id: int | None | object = _ALL) -> dict:
        """按天聚合 + 按会话聚合 + 按服务×模型聚合（近 N 天）。日期用本地时区。

        project_id 给出时只统计该项目的用量（安全审查 B14：by_session 带着
        会话标题，跨项目聚合会泄露给远程客户端）；缺省不过滤（CLI/测试/全局场景）。
        session_count 是窗口内真实去重会话数（纯计数不带标题，无项目态也可下发），
        与 by_session 的「最近 12 条」不是一回事。
        注意：date() 必须带 'unixepoch' 修饰符——ts 是 Unix 秒，
        直写 date(ts,'localtime') 在部分 SQLite 构建上会解析成错误年份。
        """
        assert self._db
        since = time.time() - days * 86400
        scoped = project_id is not _ALL
        by_day, by_session, by_provider = [], [], []
        session_count = 0
        try:
            join = (
                " FROM usage_log l JOIN sessions s ON s.id = l.session_id"
                if scoped else " FROM usage_log l"
            )
            cond = " WHERE l.ts >= ?" + (" AND s.project_id = ?" if scoped else "")
            args: tuple = (since, project_id) if scoped else (since,)
            cur = await self._db.execute(
                "SELECT date(l.ts, 'unixepoch', 'localtime') AS day,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot"
                + join + cond + " GROUP BY day ORDER BY day DESC LIMIT ?",
                (*args, days),
            )
            by_day = [dict(r) for r in await cur.fetchall()]
            cur = await self._db.execute(
                "SELECT COUNT(DISTINCT session_id) AS c" + join + cond,
                args,
            )
            row = await cur.fetchone()
            session_count = int(row[0] or 0) if row else 0
            cur = await self._db.execute(
                "SELECT l.session_id AS sid, MAX(s.title) AS title,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot, MAX(l.ts) AS last_ts"
                " FROM usage_log l LEFT JOIN sessions s ON s.id = l.session_id"
                " WHERE l.ts >= ?" + (" AND s.project_id = ?" if scoped else "")
                + " GROUP BY l.session_id"
                " ORDER BY last_ts DESC LIMIT 12",
                args,
            )
            by_session = [dict(r) for r in await cur.fetchall()]
            # 按 服务×模型 分组：同一服务先后用过多个模型时（改配置、历史行），
            # MAX(model) 会任意挑一个展示；按模型拆行图例才如实。
            cur = await self._db.execute(
                "SELECT l.provider AS provider, l.model AS model,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot,"
                " SUM(l.cached_tokens) AS cached"
                + join + cond + " GROUP BY l.provider, l.model",
                args,
            )
            by_provider = [dict(r) for r in await cur.fetchall()]
        except Exception:
            pass
        return {
            "by_day": by_day,
            "by_session": by_session,
            "by_provider": by_provider,
            "session_count": session_count,
        }

    async def usage_today(self) -> int:
        """今天（本地时区零点起）累计消耗的 token 总量，供每日预算护栏判断。"""
        assert self._db
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        try:
            cur = await self._db.execute(
                "SELECT COALESCE(SUM(in_tokens + out_tokens), 0) FROM usage_log WHERE ts >= ?",
                (midnight,),
            )
            row = await cur.fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    # ---- 快捷指令（snippets）：用户自定义提示词模板，~ 菜单展示 ----

    async def list_snippets(self) -> list[dict]:
        assert self._db
        # sort_order 升序 = 手动排序（设置页拖拽 / 新条目置顶）；同值回退
        # created_at 倒序——旧库行都是 0，行为与升级前一致
        cur = await self._db.execute(
            "SELECT * FROM snippets ORDER BY sort_order ASC, created_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_snippet(self, name: str, content: str) -> dict:
        assert self._db
        # 新条目排在列表最前：sort_order 取现有最小值 - 1（空库取 0），
        # 保证「刚建的提示词马上能在 ~ 候选前几位看到」
        cur = await self._db.execute("SELECT MIN(sort_order) FROM snippets")
        row = await cur.fetchone()
        base = row[0] if row and row[0] is not None else None
        sort_order = 0.0 if base is None else float(base) - 1.0
        cur = await self._db.execute(
            "INSERT INTO snippets (name, content, created_at, sort_order) VALUES (?, ?, ?, ?)",
            (name[:40], content[:8000], time.time(), sort_order),
        )
        await self._db.commit()
        row = await (await self._db.execute(
            "SELECT * FROM snippets WHERE id = ?", (cur.lastrowid,)
        )).fetchone()
        return dict(row)

    async def reorder_snippets(self, ids: list[int]) -> int:
        """按给定顺序重写 sort_order（0..n-1）；未提及的 id 保持原值。

        设置页拖拽排序提交整份顺序；写入值离散化后，后续新建条目
        仍取 min-1 置顶，两种来源不会打架。返回实际更新的行数。"""
        assert self._db
        changed = 0
        for idx, sid in enumerate(ids):
            cur = await self._db.execute(
                "UPDATE snippets SET sort_order = ? WHERE id = ?",
                (float(idx), int(sid)),
            )
            changed += cur.rowcount
        await self._db.commit()
        return changed

    async def mark_snippet_used(self, snippet_id: int) -> bool:
        """记一次插入使用：计数 +1、刷新最近使用时间（设置页展示用）。"""
        assert self._db
        cur = await self._db.execute(
            "UPDATE snippets SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
            (time.time(), snippet_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def update_snippet(self, snippet_id: int, name: str, content: str) -> bool:
        assert self._db
        cur = await self._db.execute(
            "UPDATE snippets SET name = ?, content = ? WHERE id = ?",
            (name[:40], content[:8000], snippet_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def delete_snippet(self, snippet_id: int) -> bool:
        assert self._db
        cur = await self._db.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
        await self._db.commit()
        return cur.rowcount > 0

    # ---- 定时任务（cron_tasks）：无人值守的周期 Agent 任务 ----

    async def list_cron_tasks(self, project_id: int | None = None) -> list[dict]:
        assert self._db
        if project_id is None:
            cur = await self._db.execute("SELECT * FROM cron_tasks ORDER BY created_at DESC")
        else:
            cur = await self._db.execute(
                "SELECT * FROM cron_tasks WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            )
        rows = await cur.fetchall()
        return [self._cron_row(r) for r in rows]

    async def get_cron_task(self, task_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM cron_tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return self._cron_row(row) if row else None

    @staticmethod
    def _cron_row(r) -> dict:
        return {
            "id": r["id"],
            "project_id": r["project_id"],
            "name": r["name"],
            "prompt": r["prompt"],
            "schedule_type": r["schedule_type"],
            "interval_minutes": r["interval_minutes"],
            "time_of_day": r["time_of_day"],
            "weekday": r["weekday"],
            "allowed_tools": [t for t in (r["allowed_tools"] or "").split(",") if t],
            "enabled": bool(r["enabled"]),
            "last_run_at": r["last_run_at"],
            "last_status": r["last_status"],
            "last_result": r["last_result"],
            "next_run_at": r["next_run_at"],
            "created_at": r["created_at"],
        }

    async def add_cron_task(
        self,
        project_id: int,
        name: str,
        prompt: str,
        schedule_type: str,
        interval_minutes: int = 0,
        time_of_day: str = "",
        weekday: int = -1,
        allowed_tools: list[str] | None = None,
    ) -> dict:
        assert self._db
        tools = ",".join(allowed_tools or [])
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO cron_tasks (project_id, name, prompt, schedule_type, interval_minutes,"
            " time_of_day, weekday, allowed_tools, created_at, next_run_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, name, prompt, schedule_type, interval_minutes, time_of_day,
             weekday, tools, now, 0.0),
        )
        await self._db.commit()
        return await self.get_cron_task(cur.lastrowid)

    async def update_cron_task(self, task_id: int, **kw) -> dict | None:
        assert self._db
        allowed = ("name", "prompt", "schedule_type", "interval_minutes", "time_of_day",
                   "weekday", "allowed_tools", "enabled", "next_run_at",
                   "last_run_at", "last_status", "last_result")
        fields, args = [], []
        for k, v in kw.items():
            if k not in allowed:
                continue
            if k == "allowed_tools":
                v = ",".join(v or [])
            fields.append(f"{k} = ?")
            args.append(v)
        if not fields:
            return await self.get_cron_task(task_id)
        args.append(task_id)
        await self._db.execute(
            f"UPDATE cron_tasks SET {', '.join(fields)} WHERE id = ?", args
        )
        await self._db.commit()
        return await self.get_cron_task(task_id)

    async def delete_cron_task(self, task_id: int) -> bool:
        assert self._db
        cur = await self._db.execute("DELETE FROM cron_tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def due_cron_tasks(self, now: float | None = None) -> list[dict]:
        """到点且启用的任务（按 next_run_at 升序）。"""
        assert self._db
        n = now if now is not None else time.time()
        cur = await self._db.execute(
            "SELECT * FROM cron_tasks WHERE enabled = 1 AND next_run_at > 0 AND next_run_at <= ?"
            " ORDER BY next_run_at ASC",
            (n,),
        )
        rows = await cur.fetchall()
        return [self._cron_row(r) for r in rows]

    # ---- 任务编排（pipelines / pipeline_nodes）：按依赖顺序自动跑的 Agent 任务 ----
    # 流水线是「草稿 → 运行 → 终态」的容器；节点是 DAG 上的一个 headless 运行，
    # 依赖满足（dep_mode: all/any）才就绪，由服务层编排循环调度执行。

    # 规模上限：并发调度兜住的是「同时跑多少」，这里兜的是「一共排多少」——
    # 没有限制时一条几千节点的流水线会让列表渲染和每 5 秒的扫描都变慢
    MAX_PIPELINE_NODES = 50
    MAX_NODE_PROMPT_CHARS = 10_000
    MAX_NODE_TITLE_CHARS = 200
    MAX_PIPELINE_NAME_CHARS = 100

    @staticmethod
    def _pipeline_row(r) -> dict:
        return {
            "id": r["id"],
            "project_id": r["project_id"],
            "name": r["name"],
            "status": r["status"],
            "concurrency": r["concurrency"],
            "created_at": r["created_at"],
            "finished_at": r["finished_at"],
        }

    @staticmethod
    def _pipeline_node_row(r) -> dict:
        return {
            "id": r["id"],
            "pipeline_id": r["pipeline_id"],
            "seq": r["seq"],
            "title": r["title"],
            "prompt": r["prompt"],
            "allowed_tools": [t for t in (r["allowed_tools"] or "").split(",") if t],
            "depends_on": [
                int(x) for x in (r["depends_on"] or "").split(",") if x.strip().lstrip("-").isdigit()
            ],
            "dep_mode": r["dep_mode"],
            "kind": r["kind"],
            "ref_id": r["ref_id"],
            "control": r["control"] if "control" in r.keys() else "",
            "max_runs": r["max_runs"] if "max_runs" in r.keys() else 1,
            "timeout_s": r["timeout_s"] if "timeout_s" in r.keys() else 3600,
            "status": r["status"],
            "result": r["result"],
            "last_error": r["last_error"],
            "session_id": r["session_id"],
            "runs": r["runs"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
        }

    async def list_pipelines(self, project_id: int | None = None) -> list[dict]:
        """流水线列表（含全部节点），新建序倒序。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute("SELECT * FROM pipelines ORDER BY created_at DESC, id DESC")
        else:
            cur = await self._db.execute(
                "SELECT * FROM pipelines WHERE project_id = ? ORDER BY created_at DESC, id DESC",
                (project_id,),
            )
        prows = await cur.fetchall()
        out = []
        for pr in prows:
            pipe = self._pipeline_row(pr)
            pipe["nodes"] = await self.list_pipeline_nodes(pipe["id"])
            out.append(pipe)
        return out

    async def get_pipeline(self, pipeline_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        pipe = self._pipeline_row(row)
        pipe["nodes"] = await self.list_pipeline_nodes(pipeline_id)
        return pipe

    async def get_pipeline_node(self, node_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM pipeline_nodes WHERE id = ?", (node_id,))
        row = await cur.fetchone()
        return self._pipeline_node_row(row) if row else None

    async def list_pipeline_nodes(self, pipeline_id: int) -> list[dict]:
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM pipeline_nodes WHERE pipeline_id = ? ORDER BY seq ASC, id ASC",
            (pipeline_id,),
        )
        rows = await cur.fetchall()
        return [self._pipeline_node_row(r) for r in rows]

    async def add_pipeline(
        self,
        project_id: int,
        name: str,
        nodes: list[dict] | None = None,
        concurrency: int = 2,
    ) -> dict:
        """建流水线（草稿态）+ 一次性落全部节点。

        nodes 里每项的 depends_on 用「同批次序号」（0 起，见 tools/pipeline.py 的
        after 字段），这里统一物化成真实节点 id——对外（WS/工具查询）只有 id 一种引用。
        """
        assert self._db
        nodes = list(nodes or [])
        if len(nodes) > self.MAX_PIPELINE_NODES:
            raise ValueError(f"一条流水线最多 {self.MAX_PIPELINE_NODES} 个节点")
        if len(name) > self.MAX_PIPELINE_NAME_CHARS:
            raise ValueError(f"流水线名不能超过 {self.MAX_PIPELINE_NAME_CHARS} 个字")
        for n in nodes:
            self._validate_node_text(n.get("title") or "", n.get("prompt") or "")
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO pipelines (project_id, name, status, concurrency, created_at)"
            " VALUES (?, ?, 'draft', ?, ?)",
            (project_id, name, max(1, min(4, int(concurrency or 2))), now),
        )
        pid = cur.lastrowid
        ids: list[int] = []
        for i, n in enumerate(nodes or []):
            deps = await self._materialize_deps(n.get("depends_on") or [], ids)
            nid = await self._insert_node(
                pid, i, n.get("title") or f"节点 {i + 1}", n.get("prompt") or "",
                n.get("allowed_tools") or [], deps, n.get("dep_mode") or "all",
                kind=n.get("kind") or "run", ref_id=str(n.get("ref_id") or ""),
                control=str(n.get("control") or ""), max_runs=int(n.get("max_runs") or 1),
                timeout_s=n.get("timeout_s") if n.get("timeout_s") is not None else 3600,
            )
            ids.append(nid)
        await self._db.commit()
        return await self.get_pipeline(pid)

    async def add_pipeline_node(
        self,
        pipeline_id: int,
        title: str,
        prompt: str,
        allowed_tools: list[str] | None = None,
        depends_on: list[int] | None = None,
        dep_mode: str = "all",
        kind: str = "run",
        ref_id: str = "",
        timeout_s: int = 3600,
    ) -> dict:
        """追加节点；depends_on 直接给真实节点 id（必须是同流水线的节点）。

        kind：run=无人值守新会话（默认）/ task=挂接任务簿任务 / session=指定会话续跑；
        task / session 节点的 ref_id 分别是任务簿任务 id 与会话 id。
        """
        assert self._db
        if kind not in ("run", "task", "session"):
            raise ValueError(f"未知节点类型：{kind}")
        self._validate_node_text(title, prompt)
        existing = await self.list_pipeline_nodes(pipeline_id)
        if len(existing) >= self.MAX_PIPELINE_NODES:
            raise ValueError(f"一条流水线最多 {self.MAX_PIPELINE_NODES} 个节点")
        await self._check_dep_refs(pipeline_id, depends_on or [], exclude_id=None)
        seq = max((n["seq"] for n in existing), default=-1) + 1
        nid = await self._insert_node(
            pipeline_id, seq, title, prompt, allowed_tools or [],
            depends_on or [], dep_mode, kind=kind, ref_id=ref_id,
            timeout_s=timeout_s,
        )
        await self._db.commit()
        return await self.get_pipeline_node(nid)

    @staticmethod
    def _validate_node_text(title: str, prompt: str) -> None:
        """节点文本的长度上限（超限报中文错，而不是默默截断让指令失真）。"""
        if len(title) > SessionStore.MAX_NODE_TITLE_CHARS:
            raise ValueError(f"节点名不能超过 {SessionStore.MAX_NODE_TITLE_CHARS} 个字")
        if len(prompt) > SessionStore.MAX_NODE_PROMPT_CHARS:
            raise ValueError(f"节点指令不能超过 {SessionStore.MAX_NODE_PROMPT_CHARS} 个字")

    async def _insert_node(
        self, pipeline_id: int, seq: int, title: str, prompt: str,
        allowed_tools: list[str], depends_on: list[int], dep_mode: str,
        kind: str = "run", ref_id: str = "",
        control: str = "", max_runs: int = 1, timeout_s: int = 3600,
    ) -> int:
        assert self._db
        cur = await self._db.execute(
            "INSERT INTO pipeline_nodes (pipeline_id, seq, title, prompt, allowed_tools,"
            " depends_on, dep_mode, kind, ref_id, control, max_runs, timeout_s, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'blocked')",
            (pipeline_id, seq, title, prompt, ",".join(allowed_tools),
             ",".join(str(int(x)) for x in depends_on), dep_mode, kind, ref_id,
             control, max(1, int(max_runs or 1)), max(0, int(timeout_s if timeout_s is not None else 3600))),
        )
        return cur.lastrowid

    async def _materialize_deps(self, deps: list, ids: list[int]) -> list[int]:
        """把同批次序号物化成节点 id；越界序号直接丢弃（宽松处理，避免建流水线被卡死）。"""
        out = []
        for d in deps or []:
            try:
                idx = int(d)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(ids):
                out.append(ids[idx])
        return out

    async def _check_dep_refs(
        self, pipeline_id: int, depends_on: list[int], exclude_id: int | None,
    ) -> None:
        """依赖必须是同流水线的其他节点；并做环检测（改依赖时可能成环）。"""
        valid = {
            n["id"]
            for n in await self.list_pipeline_nodes(pipeline_id)
            if n["id"] != exclude_id
        }
        for d in depends_on or []:
            if int(d) not in valid:
                raise ValueError(f"依赖的节点 #{d} 不存在（或不是本流水线的节点）")

    async def has_dependency_cycle(self, pipeline_id: int, node_id: int, new_deps: list[int]) -> bool:
        """假设把 node_id 的依赖改成 new_deps，图里是否会出现环。

        节点很少（个位数），DFS 足够。node_id 引用自己也按环处理。
        """
        graph = {
            n["id"]: [int(d) for d in n["depends_on"]]
            for n in await self.list_pipeline_nodes(pipeline_id)
        }
        graph[node_id] = [int(d) for d in new_deps]
        if node_id in graph[node_id]:
            return True
        seen: set[int] = set()

        def walk(cur: int, path: set[int]) -> bool:
            if cur in path:
                return True
            if cur in seen:
                return False
            path.add(cur)
            for nxt in graph.get(cur, []):
                if nxt in graph and walk(nxt, path):
                    return True
            path.discard(cur)
            seen.add(cur)
            return False

        return any(walk(nid, set()) for nid in list(graph))

    async def update_pipeline_node(self, node_id: int, **kw) -> dict | None:
        assert self._db
        if "title" in kw or "prompt" in kw:
            node = await self.get_pipeline_node(node_id)
            if node is not None:
                self._validate_node_text(
                    kw.get("title", node["title"]), kw.get("prompt", node["prompt"])
                )
        allowed = ("seq", "title", "prompt", "allowed_tools", "depends_on", "dep_mode",
                   "status", "result", "last_error", "session_id", "runs",
                   "started_at", "finished_at", "control", "max_runs", "timeout_s")
        fields, args = [], []
        for k, v in kw.items():
            if k not in allowed:
                continue
            if k == "allowed_tools":
                v = ",".join(str(x) for x in (v or []))
            elif k == "depends_on":
                v = ",".join(str(int(x)) for x in (v or []))
            elif k == "timeout_s":
                v = max(0, int(v or 0))  # 0 = 不限时
            fields.append(f"{k} = ?")
            args.append(v)
        if not fields:
            return await self.get_pipeline_node(node_id)
        args.append(node_id)
        await self._db.execute(
            f"UPDATE pipeline_nodes SET {', '.join(fields)} WHERE id = ?", args
        )
        await self._db.commit()
        return await self.get_pipeline_node(node_id)

    async def update_pipeline(self, pipeline_id: int, **kw) -> dict | None:
        assert self._db
        allowed = ("name", "status", "concurrency", "finished_at")
        fields, args = [], []
        for k, v in kw.items():
            if k not in allowed:
                continue
            if k == "concurrency":
                v = max(1, min(4, int(v or 2)))
            fields.append(f"{k} = ?")
            args.append(v)
        if not fields:
            return await self.get_pipeline(pipeline_id)
        args.append(pipeline_id)
        await self._db.execute(
            f"UPDATE pipelines SET {', '.join(fields)} WHERE id = ?", args
        )
        await self._db.commit()
        return await self.get_pipeline(pipeline_id)

    async def delete_pipeline_node(self, node_id: int) -> bool:
        assert self._db
        node = await self.get_pipeline_node(node_id)
        if node is None:
            return False
        # 引用了它的依赖一并摘除，避免悬空引用卡死下游判定
        for n in await self.list_pipeline_nodes(node["pipeline_id"]):
            if node_id in n["depends_on"]:
                await self.update_pipeline_node(
                    n["id"], depends_on=[d for d in n["depends_on"] if d != node_id]
                )
        await self._db.execute("DELETE FROM pipeline_nodes WHERE id = ?", (node_id,))
        await self._db.commit()
        return True

    async def delete_pipeline(self, pipeline_id: int) -> bool:
        assert self._db
        await self._db.execute(
            "DELETE FROM pipeline_nodes WHERE pipeline_id = ?", (pipeline_id,)
        )
        cur = await self._db.execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def pipeline_usage(self, pipeline_id: int) -> dict:
        """流水线的 token 用量汇总：节点产出的会话都记在 sessions 表（title 带 ⚙
        前缀），按 session_id 聚合 usage_log。用量列是新增能力，旧数据自然为 0。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT COALESCE(SUM(l.in_tokens), 0) AS in_tokens,"
            " COALESCE(SUM(l.out_tokens), 0) AS out_tokens"
            " FROM usage_log l JOIN pipeline_nodes n ON n.session_id = l.session_id"
            " WHERE n.pipeline_id = ?",
            (pipeline_id,),
        )
        row = await cur.fetchone()
        return {"in_tokens": int(row["in_tokens"]), "out_tokens": int(row["out_tokens"])}

    async def reset_interrupted_pipelines(self) -> int:
        """应用启动时调用：上次运行中被中断的节点标为错误，就绪态退回等待。

        中断的节点不自动重跑（无人值守运行恢复执行有风险），由用户在
        「任务编排」面板手动重跑；流水线保持 running，其余节点照常调度。
        """
        assert self._db
        await self._db.execute(
            "UPDATE pipeline_nodes SET status = 'blocked' WHERE status = 'ready'"
        )
        cur = await self._db.execute(
            "UPDATE pipeline_nodes SET status = 'error',"
            " last_error = '应用重启时运行被中断，可手动重跑', finished_at = ?"
            " WHERE status = 'running'",
            (time.time(),),
        )
        await self._db.commit()
        return cur.rowcount

    @staticmethod
    def compute_next_run(row: dict, now: float | None = None) -> float:
        """按调度类型计算下次运行时间（相对 last_run_at / 当前时间）。"""
        n = now if now is not None else time.time()
        stype = row.get("schedule_type", "interval")
        if stype == "interval":
            step = max(1, int(row.get("interval_minutes") or 0))
            base = row.get("last_run_at") or n
            return base + step * 60
        try:
            hh, mm = map(int, str(row.get("time_of_day") or "00:00").split(":"))
        except ValueError:
            return n + 24 * 3600
        if stype == "daily":
            lt = time.localtime(n)
            today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
            return today if today > n else today + 24 * 3600
        if stype == "weekly":
            wd = int(row.get("weekday") if row.get("weekday") is not None else -1)
            if wd < 0:
                return n + 7 * 24 * 3600
            lt = time.localtime(n)
            today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
            days_ahead = (wd - lt.tm_wday + 7) % 7
            cand = today + days_ahead * 24 * 3600
            if cand <= n:
                cand += 7 * 24 * 3600
            return cand
        return n + 24 * 3600


def export_messages_text(messages: list[Message]) -> str:
    """导出会话为可读文本（/export 命令用）。"""
    lines = []
    for m in messages:
        if m.role == "system":
            continue
        lines.append(f"[{m.role.upper()}] {m.to_plain()}")
    return "\n\n".join(lines)


def messages_to_json(messages: list[Message]) -> str:
    return json.dumps([m.model_dump() for m in messages], ensure_ascii=False, indent=2)
