"""SQLite 会话持久化：项目 / 会话 / 消息 / 白名单规则。

消息以归一化 Message 的 JSON 形式存储；加载后可无损恢复 Agent 历史。
注意：查询一律使用占位符参数，不拼接 SQL。
"""

from __future__ import annotations

import asyncio
import logging
import shutil  # noqa: F401 - 保留模块级引用供旧调用/测试补丁定位
import sqlite3
import time  # noqa: F401 - 保留模块级引用供测试补丁 skysheep.session.store.time.strftime 定位
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

import aiosqlite

from ..messages import Message
from ..textio import sweep_stale_tmp_files
from ._store_automation import AutomationStoreMixin
from ._store_backup import (
    BackupMixin,
    _snapshot_db,
    _snapshot_or_copy,
    _stage_and_replace,
    _validate_db_file,
    parse_backup_stamp,
)
from ._store_projects import ProjectsMixin
from ._store_relay import RelayStoreMixin
from ._store_schedules import SchedulesMixin
from ._store_sessions import SessionsMixin, _message_duration_seconds
from ._store_snippets import SnippetsMixin
from ._store_types import (
    Project,
    Session,
    project_from_row,
    session_from_row,
)
from ._store_usage import UsageAdminMixin

__all__ = [
    "Project",
    "Session",
    "SessionStore",
    "export_messages_text",
    "parse_backup_stamp",
    "project_from_row",
    "session_from_row",
    "_snapshot_db",
    "_snapshot_or_copy",
    "_stage_and_replace",
    "_message_duration_seconds",
    "_validate_db_file",
]

logger = logging.getLogger("skysheep.store")

_T = TypeVar("_T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    -- 软移除：>0 = 从侧栏/项目选择器隐藏（会话保留，可恢复）；0 = 正常显示
    removed_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    -- 与 _COLUMN_MIGRATIONS 的补列语句保持一致（列序不必对齐：代码全部按列名读写）。
    -- 漏掉会让全新安装的库只能靠 ALTER 补列，迁移一旦失败运行时才炸 no such column
    tags TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    concurrency INTEGER NOT NULL DEFAULT 2,
    notify_channel INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_pipelines_status ON pipelines(status);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    tool_errors INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
CREATE INDEX IF NOT EXISTS idx_usage_session_ts ON usage_log(session_id, ts);
CREATE TABLE IF NOT EXISTS snippets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    sort_order REAL NOT NULL DEFAULT 0,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- 斜杠命令别名（不含 /；空 = 不注册命令）
    slash TEXT NOT NULL DEFAULT ''
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
    notify_channel INTEGER NOT NULL DEFAULT 0,
    -- 任务形态（定时团队，四期）：agent = 单会话 Agent（既有行为）；
    -- team = 到点按 team_template 引用的团队模板建队、AI 总管自动闭环。
    -- allowed_tools 列在 team 模式下语义是「队员预授权名单」，不新增列。
    mode TEXT NOT NULL DEFAULT 'agent',
    team_template TEXT NOT NULL DEFAULT '',
    -- 产线级模型（2026-11-04）：空 = 跟随全局；provider_name 必须已配置，
    -- model 为空表示用该服务的默认模型。团队任务的模型由模板决定，不走这两列。
    provider_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    -- 验收工位（2026-11-04）：1 = 跑完后让验收模型判一次「做成了没有」。
    -- 默认关；只读、单轮，只能把自动判定降级，不会把被拦的运行评成做成。
    review INTEGER NOT NULL DEFAULT 0,
    last_run_at REAL NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT '',
    last_result TEXT NOT NULL DEFAULT '',
    next_run_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cron_tasks_due ON cron_tasks(enabled, next_run_at);
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
-- messages 的会话内定位索引：加载/截断都按 (session_id, seq) 走。
-- 历史上这里还并排建过一个同列重复的 idx_messages_session（写放大与存储双倍），
-- 已从 SCHEMA 移除，旧库里残留的由 connect() 的迁移 DROP 掉。
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_project_created ON sessions(project_id, created_at);
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
CREATE TABLE IF NOT EXISTS map_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL DEFAULT 'phase',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    highlights TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    session_ids TEXT NOT NULL DEFAULT '[]',
    start_ts REAL NOT NULL DEFAULT 0,
    end_ts REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_map_digests_project ON map_digests(project_id, start_ts);
-- 团队频道消息全量落库（三期）：编排器每条频道消息定稿经钩子写入一行，
-- team.log 按 team_id 拉全量回放。不外键引用 sessions（team_id 是编排器
-- 建队时分配的 uuid，不是既有表主键），session_id 只作归属注记；会话删除
-- 不级联删频道史——落库的意义就是收队后仍能按 team_id 回放全程。
CREATE TABLE IF NOT EXISTS team_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL DEFAULT 0,
    from_member TEXT NOT NULL DEFAULT '',
    to_member TEXT NOT NULL DEFAULT 'all',
    msg_kind TEXT NOT NULL DEFAULT 'system',
    task_ref TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_messages_team ON team_messages(team_id, seq);
-- 接力（无工具串行 A/B，运行期）：任务 / 节点 / 动作三张表。
-- 运行纯文本模型调用，绝不创建普通会话、绝不执行工具；节点输入输出作为
-- 明确任务记录落在这里，而不是伪装成用户会话消息（避免劫持用户会话）。
-- relay_tasks.session_id 只是「发起接力的锚点会话」注记，用于归属展示；
-- 接力不往该会话写消息，会话删除也不级联删接力历史（历史保留只读回放）。
CREATE TABLE IF NOT EXISTS relay_tasks (
    id TEXT PRIMARY KEY,
    project_id INTEGER,
    session_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'running',
    current_node TEXT NOT NULL DEFAULT '',
    active_slot TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    instruction_revision INTEGER NOT NULL DEFAULT 0,
    step INTEGER NOT NULL DEFAULT 0,
    owner_instance TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    model_snapshot_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relay_tasks_project ON relay_tasks(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS relay_nodes (
    task_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    terminal INTEGER NOT NULL DEFAULT 0,
    prompt TEXT NOT NULL DEFAULT '',
    report TEXT NOT NULL DEFAULT '',
    report_hash TEXT NOT NULL DEFAULT '',
    report_id TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY (task_id, node_id)
);
CREATE TABLE IF NOT EXISTS relay_actions (
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, request_id)
);
-- 接力（主副屏交替，2026-10-18 语义修订）：一个 pair 绑定两个**正常会话**
-- （主屏 = 用户当前会话，副屏 = 独立的接力会话），两侧按 current_side 轮流
-- 执行一整轮正常 Agent 对话（工具、权限门、检查点全部照常），这里只记录
-- 总任务、两屏的常驻任务、轮次与每一轮的指令和产出。
-- 与上面的无工具串行 A/B 三表并存：旧运行期保留，新界面走 pair。
CREATE TABLE IF NOT EXISTS relay_pairs (
    id TEXT PRIMARY KEY,
    project_id INTEGER,
    main_session_id TEXT NOT NULL,
    peer_session_id TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    main_task TEXT NOT NULL DEFAULT '',
    peer_task TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'idle',
    current_side TEXT NOT NULL DEFAULT 'main',
    turn_no INTEGER NOT NULL DEFAULT 0,
    auto_alternate INTEGER NOT NULL DEFAULT 0,
    verify_handoff INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 0,
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relay_pairs_session ON relay_pairs(main_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_relay_pairs_project ON relay_pairs(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS relay_turns (
    pair_id TEXT NOT NULL,
    turn_no INTEGER NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    brief TEXT NOT NULL DEFAULT '',
    output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL DEFAULT 0,
    verify_state TEXT NOT NULL DEFAULT '',
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    restatement TEXT NOT NULL DEFAULT '',
    verify_note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pair_id, turn_no)
);
CREATE TABLE IF NOT EXISTS automation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    pipeline_id INTEGER,
    project_id INTEGER,
    session_id TEXT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    duration_s REAL,
    result_excerpt TEXT DEFAULT '',
    error TEXT DEFAULT '',
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    denied_count INTEGER NOT NULL DEFAULT 0,
    tool_errors INTEGER NOT NULL DEFAULT 0,
    denied_tools TEXT NOT NULL DEFAULT '',
    error_kind TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    outcome_source TEXT NOT NULL DEFAULT '',
    config_snapshot TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_project
    ON automation_runs(project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_task
    ON automation_runs(task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_status ON automation_runs(status);
CREATE INDEX IF NOT EXISTS idx_automation_runs_started
    ON automation_runs(started_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS factory_acks (
    line_key TEXT PRIMARY KEY,
    acked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS factory_alerts (
    line_key TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    alerted_at REAL NOT NULL
);
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

# 旧库补列清单：(表名, 列定义)。新库由 SCHEMA 的 CREATE TABLE 直接建出全部列
# （test_store.py 断言 SCHEMA 与本清单逐列一致，两处必须同步维护）；本清单只对
# 旧库生效——connect() 时用 PRAGMA table_info 预收集已有列，缺哪列才补哪列。
# 此前是每列一段「try: ALTER ... except Exception: pass」——「列已存在」与
# disk I/O error / database locked 等真实故障一起被吞（缺列的库拖到运行时
# INSERT 才报错），且每次启动把全部 ALTER 白重跑一遍。
_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    # v13：项目软移除（removed_at）+ 提示词斜杠别名（slash）
    ("projects", "removed_at REAL NOT NULL DEFAULT 0"),
    ("snippets", "slash TEXT NOT NULL DEFAULT ''"),
    ("sessions", "pinned INTEGER NOT NULL DEFAULT 0"),
    ("schedules", "remind_before INTEGER NOT NULL DEFAULT 0"),
    ("schedules", "end_at REAL NOT NULL DEFAULT 0"),
    ("sessions", "summary TEXT NOT NULL DEFAULT ''"),
    ("sessions", "tags TEXT NOT NULL DEFAULT ''"),
    ("sessions", "archived INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "memory_digested INTEGER NOT NULL DEFAULT 0"),
    ("pipeline_nodes", "kind TEXT NOT NULL DEFAULT 'run'"),
    ("pipeline_nodes", "ref_id TEXT NOT NULL DEFAULT ''"),
    ("pipeline_nodes", "control TEXT NOT NULL DEFAULT ''"),
    ("pipeline_nodes", "max_runs INTEGER NOT NULL DEFAULT 1"),
    ("pipeline_nodes", "timeout_s INTEGER NOT NULL DEFAULT 3600"),
    ("snippets", "sort_order REAL NOT NULL DEFAULT 0"),
    ("snippets", "use_count INTEGER NOT NULL DEFAULT 0"),
    ("snippets", "last_used_at REAL NOT NULL DEFAULT 0"),
    ("snippets", "enabled INTEGER NOT NULL DEFAULT 1"),
    ("usage_log", "cached_tokens INTEGER NOT NULL DEFAULT 0"),
    ("whitelist_rules", "enabled INTEGER NOT NULL DEFAULT 1"),
    ("whitelist_rules", "hit_count INTEGER NOT NULL DEFAULT 0"),
    ("whitelist_rules", "last_hit_at REAL NOT NULL DEFAULT 0"),
    ("cron_tasks", "notify_channel INTEGER NOT NULL DEFAULT 0"),
    ("cron_tasks", "mode TEXT NOT NULL DEFAULT 'agent'"),
    ("cron_tasks", "team_template TEXT NOT NULL DEFAULT ''"),
    ("cron_tasks", "provider_name TEXT NOT NULL DEFAULT ''"),
    ("cron_tasks", "model TEXT NOT NULL DEFAULT ''"),
    ("cron_tasks", "review INTEGER NOT NULL DEFAULT 0"),
    ("pipelines", "notify_channel INTEGER NOT NULL DEFAULT 0"),
    ("automation_runs", "in_tokens INTEGER NOT NULL DEFAULT 0"),
    ("automation_runs", "out_tokens INTEGER NOT NULL DEFAULT 0"),
    ("automation_runs", "cached_tokens INTEGER NOT NULL DEFAULT 0"),
    ("automation_runs", "denied_count INTEGER NOT NULL DEFAULT 0"),
    ("automation_runs", "denied_tools TEXT NOT NULL DEFAULT ''"),
    ("automation_runs", "error_kind TEXT NOT NULL DEFAULT ''"),
    ("automation_runs", "outcome TEXT NOT NULL DEFAULT ''"),
    ("automation_runs", "outcome_source TEXT NOT NULL DEFAULT ''"),
    ("automation_runs", "config_snapshot TEXT NOT NULL DEFAULT ''"),
    ("automation_runs", "review_note TEXT NOT NULL DEFAULT ''"),
    ("relay_pairs", "verify_handoff INTEGER NOT NULL DEFAULT 1"),
    ("relay_turns", "verify_state TEXT NOT NULL DEFAULT ''"),
    ("relay_turns", "verify_attempts INTEGER NOT NULL DEFAULT 0"),
    ("relay_turns", "restatement TEXT NOT NULL DEFAULT ''"),
    ("relay_turns", "verify_note TEXT NOT NULL DEFAULT ''"),
    # v14：usage_log 增轮级质量口径（每轮一行，见 add_usage）——tool_calls /
    # tool_errors 支撑「工具报错率」，failed 支撑「轮成功率」；
    # automation_runs 增 tool_errors（带病跑完的无效步骤信号，巡检消化）。
    ("automation_runs", "tool_errors INTEGER NOT NULL DEFAULT 0"),
    ("usage_log", "tool_calls INTEGER NOT NULL DEFAULT 0"),
    ("usage_log", "tool_errors INTEGER NOT NULL DEFAULT 0"),
    ("usage_log", "failed INTEGER NOT NULL DEFAULT 0"),
)

# 数据库结构版本：SCHEMA 或 _COLUMN_MIGRATIONS 每次变更时 +1。
# v2：新增 relay_tasks / relay_nodes / relay_actions（接力运行期三表）。
# v3：新增 relay_pairs / relay_turns（主副屏交替接力）。
# v4：新增 automation_runs（自动化运行账本：定时任务 / 编排节点每次运行一行）。
# v5：automation_runs 增 in/out/cached_tokens（单次运行成本）；新增 factory_acks
#     （「需留意」的已处理标记：按产线记一个时间戳，更晚的失败会重新浮上来）。
# v6：automation_runs 增 denied_count（本轮被无人值守门控拒绝的工具次数）/
#     error_kind（失败归因分类）/ outcome 与 outcome_source（「跑完」之外的
#     「做成」判定：自动启发式 + 人工标记）；新增 factory_alerts
#     （失败聚合告警的去重：同一产线同一天最多告警一次）。
# v7：automation_runs 增 denied_tools（被拦工具名与次数的 JSON 快照）——
#     只记次数无法告诉用户该补哪个预授权工具。
# v8：cron_tasks 增 mode（任务形态：agent 单会话 / team 定时团队）与
#     team_template（团队模板引用名）——定时团队（团队模式设计 §14）。
# v9：automation_runs 增 config_snapshot（开跑时的提示词 / 预授权 / 形态快照）——
#     配置是原地改的，历史运行要能回答「当时用的是哪一版」。
# v10：cron_tasks 增 provider_name / model（产线级模型：空 = 跟随全局）——
#     日报用便宜模型、周报用强模型，不同活可以分给不同的服务跑。
# v11：cron_tasks 增 review（验收工位开关，默认关）；automation_runs 增
#     review_note（验收结论 / 未完成原因）——「跑完」之外再补一次只读验收。
# v12：relay_pairs 增 verify_handoff（自动交替换手前先做复述核验，默认开）；
#     relay_turns 增 verify_state / verify_attempts / restatement / verify_note
#     （通过核验的那次复述与过程留痕，接力模式设计 §6 的类型化交接记录）。
# v13：projects 增 removed_at（软移除）；snippets 增 slash（斜杠命令别名）。
# v14：usage_log 增 tool_calls / tool_errors / failed（轮级质量口径，用量页
#     出「轮成功率 / 工具报错率」）；automation_runs 增 tool_errors（巡检
#     看「带病跑完」）。
# 写入 PRAGMA user_version，用于降级检测——旧版程序打开新版库时，缺列补列
# 只能「新版本向前兼容旧库」，反向没有保障（新列在新代码里可能有 NOT NULL
# 语义，旧程序写出来的行会破约束）；拒绝打开并把后果说清楚，比静默写坏强。
SCHEMA_VERSION = 14


class _SharedConnRetry:
    """共享连接的瞬时锁失败重试壳。

    M16 起多语句写走独立连接（_write_txn/_immediate_txn），共享连接上的单语句
    写开始与它们竞争写锁。实测（pipeline 并发节点压测）：共享连接的 DML 在
    独立事务提交的瞬间可能拿到**绕过 busy_timeout 的即时 locked 失败**（延迟
    事务快照过期一类，busy handler 对这类场景不生效），且失败后隐式事务停在
    共享连接上，把后续所有语句一起拖死。这里对这类瞬时失败做有限次退避重试；
    重试仍失败则回滚收场再上抛——单语句写方法里该事务只含失败语句本身，
    回滚即「本方法未完成」的如实语义，也不会把毒事务留给后续方法。

    只代理 execute / executemany（写语义所在），其余属性透传原始连接。
    """

    def __init__(self, conn: aiosqlite.Connection,
                 retries: int = 3, delay: float = 0.02) -> None:
        self._conn = conn
        self._retries = retries
        self._delay = delay

    async def execute(self, sql, *args, **kwargs):
        delay = self._delay
        for attempt in range(self._retries + 1):
            try:
                return await self._conn.execute(sql, *args, **kwargs)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                transient = "locked" in msg or "busy" in msg
                if not transient:
                    raise
                # 每次都先回滚再重试：首次失败时共享连接上已经留下一个拿不到
                # 写锁的隐式事务，不清理的话后续重试全撞在同一份过期快照上
                # （WAL 里「读快照升级为写」的 busy 不经过 busy handler，重试
                # 只是白白撞墙），表现为 4 次重试在 0.1 秒内全部即时失败。
                try:
                    await self._conn.rollback()
                except Exception:  # noqa: BLE001 - 回滚失败不掩盖原异常
                    pass
                if attempt >= self._retries:
                    logger.warning("共享连接写重试 %d 次仍失败，回滚收场：%s", attempt + 1, e)
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    async def executemany(self, sql, *args, **kwargs):
        delay = self._delay
        for attempt in range(self._retries + 1):
            try:
                return await self._conn.executemany(sql, *args, **kwargs)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                transient = "locked" in msg or "busy" in msg
                if not transient:
                    raise
                # 与 execute 同款：先清掉失败语句留下的隐式事务再重试
                try:
                    await self._conn.rollback()
                except Exception:  # noqa: BLE001 - 回滚失败不掩盖原异常
                    pass
                if attempt >= self._retries:
                    logger.warning("共享连接 executemany 重试 %d 次仍失败，回滚收场：%s",
                                   attempt + 1, e)
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    def __getattr__(self, name):
        return getattr(self._conn, name)


class SessionStore(
    AutomationStoreMixin,
    RelayStoreMixin,
    SchedulesMixin,
    SnippetsMixin,
    BackupMixin,
    ProjectsMixin,
    SessionsMixin,
    UsageAdminMixin,
):
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
        # 常驻独立写连接（_immediate_txn 用）：首版每条写事务都现场
        # connect/close 一次，append_message 这类一消息一事务的路径在慢盘
        # （CI runner）上把连接开销放大成 pytest-timeout。连接本身不便宜，
        # 复用一条；并发仍由 asyncio.Lock + BEGIN IMMEDIATE 串行，语义不变。
        self._writer: aiosqlite.Connection | None = None
        self._writer_lock: asyncio.Lock = asyncio.Lock()
        self._writer_owner: asyncio.Task | None = None
        self._relay_peer_lock: asyncio.Lock = asyncio.Lock()
        self.backup_created: str | None = None
        # FTS5 全文索引是否就绪（断开后 connect 会重新判定）；
        # False 时 search_messages 退到 LIKE 扫描，功能不降级只是变慢。
        self.fts_ready = False

    async def connect(self) -> SessionStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 启动清扫：回收数据目录内 textio 原子写被硬杀/断电留下的 .tmp 残留
        # （db 与引擎状态文件都在数据目录里；用户项目目录不扫，取舍见
        # textio.sweep_stale_tmp_files）。卫生措施，失败只记日志不拦启动。
        try:
            sweep_stale_tmp_files(self.path.parent)
        except Exception as e:  # noqa: BLE001 - 清扫绝不能影响启动
            logger.warning("启动清扫 .tmp 残留失败（不影响使用）：%s", e)
        # 文件是否本就存在：本次新建的库没有可备的内容，跳过备份（与旧版
        # 「连接前备份」对首启的语义一致）
        existed = self.path.exists()
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # WAL + NORMAL：一轮对话多次小写的 fsync 次数大幅下降，读不再被
        # 写事务的 journal 排他锁挡住；busy_timeout 兜住偶发的写写碰撞。
        # journal_mode 是库级持久属性，synchronous/busy_timeout 是连接级的，
        # 所以每次 connect 都要设全这三样。
        cur_jm = await self._db.execute("PRAGMA journal_mode=WAL")
        jm_row = await cur_jm.fetchone()
        self.journal_mode = str(jm_row[0]).lower() if jm_row and jm_row[0] else ""
        if self.journal_mode and self.journal_mode != "wal":
            logger.warning(
                "会话库未进入 WAL 模式（实际模式=%s，路径=%s），并发写入可能退化",
                self.journal_mode, self.path,
            )
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        # 降级防护：库里版本号比程序新，说明这个库被更新版本的 SkySheep 打开过。
        # 旧程序没有对应列的语义认知，继续写可能破约束/写坏数据，明确拒绝。
        cur = await self._db.execute("PRAGMA user_version")
        db_version = int((await cur.fetchone())[0])
        if db_version > SCHEMA_VERSION:
            await self._db.close()
            self._db = None
            raise RuntimeError(
                f"会话数据库由更新版本的 SkySheep 创建（库版本 {db_version} > "
                f"当前程序支持 {SCHEMA_VERSION}），降级打开可能损坏数据。"
                "请升级 SkySheep，或用备份文件恢复（设置 → 数据管理 → 备份与恢复）。"
            )
        # 滚动备份要在连接建立、WAL checkpoint 之后做（数据才完整），见 _rolling_backup
        await self._rolling_backup(existed)
        await self._db.executescript(SCHEMA)
        migrated = await self._migrate_legacy()
        if db_version < SCHEMA_VERSION:
            if existed:
                # 版本升格前先留一份 pre-migrate 备份：迁移不可逆（补列只进不退），
                # 万一新版本有问题，用户还能拿这份回到旧版程序。全新库（existed=False）
                # 没有可迁的旧数据，不备份。备份失败不拦启动（滚动备份已在上面做过
                # 一次），只是少了这层额外保障。
                try:
                    await self._pre_migrate_backup()
                except Exception as e:  # noqa: BLE001
                    logger.warning("迁移前备份失败（不影响启动）：%s", e)
            if migrated:
                await self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                # 补列失败时不盖版本号：user_version 宣称迁移完成而实际列缺失，
                # 只会把问题推迟到运行时 no such column。保持旧版本号即保持
                # 「迁移未完成」语义，下次启动自然重试补列（迁移幂等）。
                logger.warning(
                    "旧库迁移未全部完成，user_version 保持 %d 不盖章（下次启动重试迁移）",
                    db_version,
                )
        await self._db.commit()
        # M16：共享连接套上瞬时锁失败重试壳（_SharedConnRetry）。放在全部
        # 连接级 pragma / row_factory 设置之后——壳只代理 execute / executemany，
        # 其余属性（commit / close / in_transaction / row_factory…）仍透传原始连接。
        self._db = _SharedConnRetry(self._db)
        self._fts_dirty = False  # 索引写失败过 → 下次启动走查漏模式
        await self._setup_fts()
        # 库文件来源留痕：排查「这一轮为什么没落库」先看用的是哪个库。
        # 两个实例用不同 SKYSHEEP_HOME 时，日志里的路径是唯一能立刻分辨的线索。
        logger.info(
            "会话库已连接：%s（%s，库版本 %s/%s）",
            self.path, "已有库" if existed else "本次新建",
            db_version, SCHEMA_VERSION,
        )
        return self

    async def _pre_migrate_backup(self) -> None:
        """schema 版本升格前把当前库快照一份到 backups/pre-migrate-<版本>.db。

        与滚动备份同目录、同 backup API 一致性快照，不占 BACKUP_KEEP 名额
        （按文件名前缀区分，restore_backup 只认主前缀，不会被误恢复成主库）。
        """
        backup_dir = self.backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"pre-migrate-v{SCHEMA_VERSION}-{self.path.stem}.db"
        if target.exists():
            target.unlink()
        await asyncio.to_thread(_snapshot_or_copy, self.path, target)

    async def _migrate_legacy(self) -> bool:
        """旧库迁移：缺列才补列 + 清理历史上的重复索引。返回列迁移是否全部成功。

        补列先按 PRAGMA table_info 逐表收集已有列，只对缺失的列发 ALTER；
        非「列已存在」的故障（disk I/O error / database locked 等）不再静默
        吞掉，记 warning 暴露出来——缺列的库此前要拖到运行时 INSERT 才报错，
        很难排查。补列失败时返回 False，调用方（connect）据此不把 user_version
        盖到最新：版本号宣称迁移完成而列缺失，等于把问题推迟到运行时
        no such column；留着旧版本号，下次启动自然重试补列（按 PRAGMA 收
        缺列，幂等）。重复索引清理失败只影响写放大，不阻塞版本盖章。
        """
        assert self._db
        tables = sorted({table for table, _ in _COLUMN_MIGRATIONS})
        existing: dict[str, set[str]] = {}
        for table in tables:
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            existing[table] = {row[1] for row in await cur.fetchall()}
        columns_ok = True
        for table, ddl in _COLUMN_MIGRATIONS:
            columns = existing.get(table)
            if not columns:
                continue  # 表不存在：SCHEMA 刚按最新结构建好，无需补列
            column = ddl.split(None, 1)[0]
            if column in columns:
                continue
            try:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            except Exception as e:  # noqa: BLE001 - 单列失败不拦启动，但要可见
                columns_ok = False
                logger.warning("旧库迁移失败：%s 补列 %s 未执行（%s）", table, column, e)
        # messages 一度并存两个定义完全相同的索引（idx_messages_session 与
        # idx_messages_session_seq），最大的表写放大与存储双倍；SCHEMA 只保留
        # 后者，旧库的残留在这里清掉。
        try:
            await self._db.execute("DROP INDEX IF EXISTS idx_messages_session")
        except Exception as e:  # noqa: BLE001 - 清理失败不影响功能，但要可见
            logger.warning("旧库迁移失败：重复索引 idx_messages_session 未清理（%s）", e)
        return columns_ok

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
        except Exception:  # noqa: BLE001
            return  # 没有 FTS5 支持：保留 LIKE 扫描路径
        try:
            await self._backfill_fts()
        except Exception:  # noqa: BLE001
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
        inserted_total = 0
        while True:
            rows = await cur.fetchmany(500)
            if not rows:
                break
            payload = [(int(r["id"]), self._plain_text(r["content"])) for r in rows]
            await self._db.executemany(
                "INSERT INTO messages_fts(rowid, plain) VALUES (?, ?)", payload
            )
            inserted_total += len(payload)
        if inserted_total:
            await self._db.commit()
            logger.info("FTS 索引补齐 %d 条消息（%s）", inserted_total,
                        "查漏模式" if self._fts_dirty else "水位增量")
        self._fts_dirty = False

    @staticmethod
    def _plain_text(raw: str) -> str:
        """消息 JSON → 可搜索的纯文本（与旧 LIKE 搜索的可见内容一致）。"""
        try:
            return Message.model_validate_json(raw).to_plain()
        except Exception:  # noqa: BLE001 - 坏行退化成原始字符串，不能让它挡住索引
            return str(raw)

    async def _fts_insert(self, conn, message_id: int, content_json: str) -> None:
        """把一条消息同步进 FTS（conn 是承载本次写入事务的连接）；未启用时无事发生。

        写失败不影响主存储，但要留痕并置脏：静默吞掉会让这条消息永久搜不到
        （水位法补不回它），脏标记让下次启动走查漏模式修复（安全审查低危项）。
        调用方把 FTS 语句与主表语句放进同一写事务——主表失败回滚时已写的
        索引行随之回滚，不会与主表漂移。
        """
        if not self.fts_ready:
            return
        try:
            await conn.execute(
                "INSERT INTO messages_fts(rowid, plain) VALUES (?, ?)",
                (int(message_id), self._plain_text(content_json)),
            )
        except Exception as e:  # noqa: BLE001
            self._fts_dirty = True
            logger.warning("FTS 索引写入失败（消息 %s，下次启动补齐）：%s", message_id, e)

    async def _fts_delete(self, conn, where_sql: str, args: tuple) -> None:
        """按 messages 的命中行同步删除 FTS 行（where_sql 作用于 messages 子查询）。

        conn 是承载本次写入事务的连接；必须在主表删除之前调用（子查询还要
        借 messages 现存行圈定要删的索引行）。主表删除失败回滚时，这里已删的
        索引行随事务一起回滚，不会再出现「索引删了、消息还在」的漂移。
        """
        if not self.fts_ready:
            return
        try:
            await conn.execute(
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
        await self._close_writer()
        if self._db:
            await self._db.close()
            self._db = None

    async def _close_writer(self) -> None:
        """收掉常驻写连接。close() 与备份恢复（换库文件）前都必须走。"""
        async with self._writer_lock:
            if self._writer:
                try:
                    await self._writer.close()
                except Exception:  # noqa: BLE001 - 关闭失败不掩盖原流程
                    logger.warning("关闭独立写连接失败（忽略）：%s", self.path.name)
                self._writer = None
                self._writer_owner = None

    # ---- 独立写连接事务（多语句全有全无） ----
    #
    # 共享 self._db（aiosqlite 默认 isolation_level="" 隐式延迟事务）上的多语句
    # 写没有事务边界：其他协程的 commit() 会把本方法的中间状态提交落盘，方法
    # 中途失败也不回滚，留下半完成状态（半删除的项目、新旧交错的演化摘要、
    # FTS 与主表漂移）。凡「多条写语句必须同生同死」的路径都走这里：在常驻
    # 独立写连接上 BEGIN IMMEDIATE 拿写锁——事务内其他协程既插不进语句也提交
    # 不了，异常整体回滚、成功一次提交。连接常驻不重开（首版一事务一连接，
    # append_message 一消息一事务，连接开销在慢盘上被放大成 CI 超时），协程
    # 间串行由 asyncio.Lock 承担，SQLite 写锁由 BEGIN IMMEDIATE 承担。纪律与
    # 接力表相同：回调内只做本地 SQL，绝不 await provider / 窗口 / 其他会自行
    # commit 的存储 API，也绝不再进任何 _write_txn（锁不可重入，嵌套即死锁，
    # 见下面的显式防御）。单语句写不需要这里（SQLite 单语句本就原子）。

    @asynccontextmanager
    async def _immediate_txn(self):
        """独立短写事务骨架：常驻写连接 + BEGIN IMMEDIATE，正常出口提交、异常回滚。

        pragmas 与主连接同口径：busy_timeout=5000 兜写写碰撞；journal_mode 是
        库级持久属性、连接建一次即可，synchronous 是连接级、归一到主连接的
        NORMAL（WAL 下安全且 fsync 次数与主连接一致）。回滚失败不掩盖原异常。
        """
        if self._writer_lock.locked() and self._writer_owner is asyncio.current_task():
            raise RuntimeError(
                "写事务回调内又调了存储写方法（嵌套 _write_txn）——纪律禁止："
                "回调内只做本地 SQL，需要另一组写入就在回调外完成或合并进本事务"
            )
        async with self._writer_lock:
            self._writer_owner = asyncio.current_task()
            try:
                conn = self._writer
                if conn is None:
                    conn = await aiosqlite.connect(self.path)
                    conn.row_factory = aiosqlite.Row
                    await conn.execute("PRAGMA busy_timeout=5000")
                    await conn.execute("PRAGMA synchronous=NORMAL")
                    self._writer = conn
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as e:
                    # 共享连接上的孤儿写事务兜底：任务在写方法的 execute 与
                    # commit 之间被取消（典型：轮次中途点停止打断 touch/落库），
                    # 隐式事务就留在共享连接上无人收尸，写锁被它占死，这里的
                    # BEGIN 等 busy_timeout 耗尽后报 locked。这份中间态本就不该
                    # 提交——它的方法从未完成、返回值无人接住，回滚即清理
                    # （正是本审计要消除的「半完成状态」），清掉后重取锁。
                    # 活跃写者的 commit 不依赖共享连接，能熬过 5 秒等待还开着
                    # 事务的只能是孤儿。写连接自身按纪律出口必 commit/rollback，
                    # 理论不会残留事务；万一报「已在事务中」按同样思路回滚重试。
                    low = str(e).lower()
                    if "locked" in low and self._db is not None and self._db.in_transaction:
                        logger.warning("清理共享连接上的孤儿写事务后重试 BEGIN（%s）", e)
                        await self._db.rollback()
                        await conn.execute("BEGIN IMMEDIATE")
                    elif "within a transaction" in low:
                        logger.warning("写连接残留未收尾事务，回滚后重试 BEGIN（%s）", e)
                        await conn.rollback()
                        await conn.execute("BEGIN IMMEDIATE")
                    else:
                        raise
                yield conn
                await conn.commit()
                # 提交后仍在事务里 = 事务没收尾（WAL/磁盘异常时 commit 可能不生效）。
                # 静默继续会让后续写入叠在孤儿事务上，最终表现为「界面上聊了半天、
                # 重启全没了」——宁可在这里显式失败并留日志。
                if conn.in_transaction:
                    await conn.rollback()
                    raise RuntimeError(
                        f"写事务提交后仍处于事务中（{self.path.name}）："
                        "数据库可能只读、被占用或写到了别的库文件"
                    )
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001 - 回滚失败不掩盖原异常
                    pass
                raise
            finally:
                self._writer_owner = None

    async def _write_txn(
        self, fn: Callable[[aiosqlite.Connection], Awaitable[_T]]
    ) -> _T:
        """在独立写事务里执行 fn(conn)，透传 fn 的返回值；fn 抛异常则整体回滚。

        多语句写方法的标准入口：把「这组语句必须同生同死」的部分包成回调，
        语句全部落进 BEGIN IMMEDIATE 事务，中间状态在提交前对其他连接不可见，
        也不会被其他协程的 commit 提前落盘。
        """
        async with self._immediate_txn() as conn:
            return await fn(conn)

    # ---- projects ----

    # 「远程连接」固定项目的哨兵路径：不是真实目录，只作 projects.root_path 的
    # 唯一键（幂等创建/识别都用它）。不指向磁盘上任何位置，永远不会被 switch。
    REMOTE_PROJECT_PATH = "//skysheep-remote"

def export_messages_text(messages: list[Message]) -> str:
    """导出会话为可读文本（/export 命令用）。"""
    lines = []
    for m in messages:
        if m.role == "system":
            continue
        lines.append(f"[{m.role.upper()}] {m.to_plain()}")
    return "\n\n".join(lines)
