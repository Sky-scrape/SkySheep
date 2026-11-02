"""跨机全量迁移（P24）：整包导出 / 导入——会话库 + 技能 + 界面偏好 + 脱敏配置。

与既有两条导出链的分工：

- ``settings.export``（``settings.import``）：只含配置类小文件，不含会话库与技能正文；
- ``data.export_full``（``data.import_full``，backend.py）：大库友好——包落盘
  ``exports/``、导入走本机文件路径，config.toml 可选择连密钥一起带；
- 本模块（``migration.export`` / ``migration.import``）：前端中转的整包迁移——
  包体 base64 走 WS（``MAX_MIGRATION_PACKAGE_BYTES`` 上限防帧爆炸），
  **config.toml 递归剥掉密钥类字段、mcp.json 整个不进包**（鉴权头是明文
  凭据，属已知取舍，见 SECURITY.md），导入是「替换本机数据」的危险操作：
  zip 条目白名单校验 + 导入前先备份本机库 + 库文件走 ``_stage_and_replace``
  原子替换。两个 WS 方法均 local_only（跨机迁移只在本机界面做）。

安全红线（导出侧）：

- 密钥不进包：config.toml 里键名含 api_key / secret / token / password 的
  行整行删除（保留其余结构与注释），脱敏后用 tomllib 复核——脱不出合法
  TOML 就拒绝导出，绝不带着写坏的配置上飞机；mcp.json 排除；
- 包内自带 manifest.json（各部分大小与文件清单），导入侧逐项比对。

安全红线（导入侧）：

- 条目白名单：只收 ``skysheep.db`` / ``config.toml`` / ``ui.json`` /
  ``manifest.json`` 与 ``skills/<技能名>/<文件>`` 形状；任何路径穿越
  （``..``）、盘符冒号、未知条目一律整体拒绝，不做静默跳过；
- 技能内容有解压预算：技能条目数与累计解压大小复用技能安装器的
  ``MAX_SKILL_FILES`` / ``MAX_UNPACKED_BYTES`` 上限，超限在扫描阶段拒绝
  （此时一个字节都没解压）——防 zip bomb 写满磁盘；
- 导入按「降防护状态」清洗（P24 安全收紧）：包内库里的自动化 / 白名单 /
  渠道绑定表（``_MIGRATION_CLEAN_TABLES``）整表清空——迁移包可能来自
  不可信机器，植入的定时任务、流水线会被在跑的 cron / 流水线循环直接
  认领执行，命令白名单命中即自动放行，渠道绑定是无人值守遥控入口；
  config.toml 的防护总开关键（``computer_control`` / ``browser_control`` /
  ``browser_ignore_cert``）与 ui.json 的 ``accept_edits`` 同款处理：
  降防护档位只能由本机用户主动切换，不随包生效。清洗了哪些表 / 键在
  导入结果里逐项披露（前端导入完成提示同步展示）；
- 导入前必须先备份本机库：复用 store 的 ``_snapshot_or_copy``（SQLite
  backup API 一致性快照，坏库退回裸拷贝留底），落 store 的 backups 目录、
  按 ``-迁移前`` 后缀标注，BACKUP_KEEP 滚动裁剪照旧；
- 库文件替换走 ``_stage_and_replace``（同目录临时文件 + quick_check +
  os.replace 原子换上）；config.toml / ui.json 走 textio 原子写；
- 技能目录**合并**还原（同名覆盖、本机多出的技能保留不删）——迁移不是
  「克隆」，清空本机技能目录的风险大于残留旧技能；被覆盖的同名技能
  先整份备份（replace 档移入备份区、merge 档复制留底），失败可还原；
- ui.json 里的 ``accept_edits``（自动允许写入）照 settings.import 同一
  防护剥掉：降防护档位只能由本机用户主动切换，不随包生效；
- 密钥红线在导入侧再兜一遍：写盘前对包里的 config.toml 重新脱敏，
  不信任「包自称已脱敏」。

忙碌保护在 mixin 层（``MigrationMixin.migration_import``）：会话/定时/
流水线在飞走 ``_backup_busy_message``，团队自动循环在飞（``_teams`` 任一
``loop_running``）单独拒绝。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path

from ... import __version__
from ...config import skysheep_home
from ...session.store import (
    SCHEMA_VERSION,
    SessionStore,
    _snapshot_or_copy,
    _stage_and_replace,
    _validate_db_file,
)
from ...skills.installer import (
    MAX_SKILL_FILES,
    MAX_UNPACKED_BYTES,
    SkillInstallError,
    _skill_target,
)
from ...textio import read_text_file, write_bytes_atomic, write_text_atomic

logger = logging.getLogger(__name__)

# 包格式版本：将来加新条目类型时 +1，旧引擎收到更高版本的包直接拒
# （与库版本降级防护同一姿态：新版产物不能被旧程序硬吃）。
MIGRATION_FORMAT_VERSION = 1

# 整包尺寸上限：包体 base64 后经 WS 传输，64MB 是防帧爆炸的经验上限。
# 超过时导出**不直接拒绝**（P24 v2）：改「落盘模式」——包写到
# ~/.skysheep/migrations/ 并回传路径。导入侧两种来源（base64 / 本机路径）
# 都受同一上限约束（安全收紧：磁盘上的恶意包同样要过尺寸关，配合技能
# 内容解压预算防 zip bomb 写满磁盘）——超过上限的大包可以导出落盘用于
# 拷贝存档，但无法再经导入链路换回本机。
MAX_MIGRATION_PACKAGE_BYTES = 64 * 1024 * 1024
# 落盘模式保留的包数量（导出目录滚动裁剪；包的用途是「拷到另一台机器」，留着旧的没意义）
MIGRATION_PACKAGE_KEEP = 5

# 技能还原口径（P24 v2）：
# - merge（默认）：同名技能**叠加覆盖**（包内文件盖掉同名文件，本机多出的文件与
#   技能都保留）——迁移不是克隆，不动用户本机别的技能；
# - replace：同名技能**整目录替换**（先把本机同名目录移到备份区，再整目录拷入），
#   避免「包里的旧文件 + 本机的新文件」混在一起变成谁也说不清的状态。
SKILLS_MODES = ("merge", "replace")

# config.toml 脱敏的键名标记（小写匹配、含即删行）：与任务口径一致的四类。
# 注意比 support.SECRET_KEYS 窄（那边还有 key/passwd/credential 等）——迁移
# 包要在另一台机器上「开箱可用」，只剥明确的密钥字段，避免误伤普通配置。
_SECRET_KEY_MARKERS = ("api_key", "secret", "token", "password")

# 包内顶层固定条目（白名单的精确名部分）
_TOP_ENTRIES = ("skysheep.db", "config.toml", "ui.json", "manifest.json")

# 导入侧按「降防护状态」整表清空的库表（固定清单，语句不拼外部输入）：
# - cron_tasks / schedules / pipelines / pipeline_nodes：无人值守自动化——
#   后端有常驻循环，在跑的 cron 与流水线扫描到 due / running 行就直接
#   认领执行，恶意包植入的任务会自动跑起来；
# - whitelist_rules：命令前缀白名单，命中即自动放行（免确认执行）；
# - channel_bindings / channel_sources：聊天渠道绑定与来源，是无人值守
#   的远程遥控入口。
# 迁移包可能来自不可信机器：这些类别的数据一律不随包生效，需要的话由
# 本机用户导入后重建（对齐 ui.json accept_edits「降防护档位不随包生效」
# 的同一语义）。清洗了哪些表（及行数）随导入结果逐项披露。
# 注意：apply_migration_package 里的清空语句按表逐一展开成字面常量
# （表名进不了 sqlite 的参数绑定），改动本清单必须同步增删那些语句。
_MIGRATION_CLEAN_TABLES = (
    "cron_tasks",
    "schedules",
    "whitelist_rules",
    "channel_bindings",
    "channel_sources",
    "pipelines",
    "pipeline_nodes",
)

# config.toml 防护总开关键（精确键名、小写匹配）：电脑控制 / 浏览器控制 /
# 证书校验忽略。导入时不随包生效——这些键打开的都是「Agent 直接动真机 /
# 放松校验」的降防护面，只能由本机用户主动切换。
_GUARD_SWITCH_KEYS = ("computer_control", "browser_control", "browser_ignore_cert")

# 导入前备份的文件名标记：落 store 的 backups 目录，界面的备份列表按
# {stem}-*.db 收录，时间戳解析不出时退回 mtime 展示（不破坏既有列表）。
_PRE_IMPORT_TAG = "-迁移前"


def sanitize_config_text(text: str) -> tuple[str, list[str]]:
    """把 config.toml 文本里密钥类键的行整行删除，返回（脱敏文本, 被删键路径）。

    逐行扫描：键名（去引号、小写）含 _SECRET_KEY_MARKERS 任一标记即删除该行，
    其余原样保留——结构与注释不动，等价于「删除该键、保留结构」。行内值
    一律不看内容（密钥值一个字符都不进包）。同时跟踪 [表头] 记录被删键的
    点分路径，供 manifest 带回、导入后提示「哪些服务的密钥需要重填」。
    """
    out: list[str] = []
    stripped: list[str] = []
    table = ""
    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("[") and stripped_line.endswith("]"):
            table = stripped_line.strip("[]").strip().strip('"')
            out.append(line)
            continue
        if "=" in stripped_line and not stripped_line.startswith("#"):
            key = stripped_line.split("=", 1)[0].strip().strip('"').lower()
            if any(marker in key for marker in _SECRET_KEY_MARKERS):
                name = stripped_line.split("=", 1)[0].strip().strip('"')
                stripped.append(f"{table}.{name}" if table else name)
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if out else ""), stripped


def _strip_guard_switch_keys(text: str) -> tuple[str, list[str]]:
    """把 config.toml 文本里防护总开关键的「键值行」整行删除，返回（清洗文本, 被删键名）。

    逐行匹配两类形状（键名先去单/双引号、小写）：
    - 精确键名：``computer_control = true``；
    - 点分键前缀：``computer_control.x = true``——点分子键会让解析结果里
      出现非空的 computer_control 表，加载侧 ``bool(raw.get(key, False))``
      对非空表照样取真，等于把开关打开，必须一并剥。
    键在哪个 [表] 下都删（这些名字是引擎保留的开关名，嵌套出现同样不是
    正常配置该有的形状）。防误伤：匹配「等于键名」或「键名 + 点」的前缀，
    不做「含即删」（computer_control_extra 之类不受影响）。

    表形态（``[computer_control]`` 段头）**不**在此处按行处理：逐行扫描
    分不清它是真段头还是多行字符串里的文本，误剥会损坏正常配置——该形状
    由 apply_migration_package 在 tomllib 解析之后做 fail-closed 兜底
    （解析结构里出现这三个键即整体拒绝；tomllib 的解析是权威的，字符串
    内容不会被误判成表）。
    """
    out: list[str] = []
    stripped: list[str] = []
    dotted_prefixes = tuple(name + "." for name in _GUARD_SWITCH_KEYS)
    for line in text.splitlines():
        stripped_line = line.strip()
        if "=" in stripped_line and not stripped_line.startswith("#"):
            key = stripped_line.split("=", 1)[0].strip().strip("\"'").lower()
            if key in _GUARD_SWITCH_KEYS or key.startswith(dotted_prefixes):
                stripped.append(key.split(".", 1)[0])
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if out else ""), stripped


def _entry_parts_clean(parts: list[str]) -> bool:
    """zip 条目路径逐段检查：空段 / 点 / 点点 / 盘符冒号（zip slip 两套变形）全挡。"""
    return all(p not in ("", ".", "..") and ":" not in p for p in parts)


def _skill_name_ok(name: str, root: Path) -> bool:
    """包内 skills/<名>/ 的一级目录名是否满足安装器同款校验（不合法即拒）。"""
    try:
        _skill_target(root, name)
    except SkillInstallError:
        return False
    return True


def _list_skill_files(skills_root: Path) -> list[tuple[str, bytes]]:
    """收 skills/ 下可迁移的技能文件：skills/<名>/<文件…> 形状，散文件不收。

    一级目录名过安装器同款校验（不合法的整目录跳过，保证导出的包一定能被
    本模块导入）；.backups 目录（历史遗留的技能备份位）不进包。
    """
    files: list[tuple[str, bytes]] = []
    if not skills_root.is_dir():
        return files
    for f in sorted(skills_root.rglob("*")):
        if not f.is_file() or ".backups" in f.parts:
            continue
        rel = f.relative_to(skills_root).as_posix()
        parts = rel.split("/")
        if len(parts) < 2 or not _entry_parts_clean(parts):
            continue
        if not _skill_name_ok(parts[0], skills_root):
            continue
        try:
            files.append((f"skills/{rel}", f.read_bytes()))
        except OSError as e:
            raise RuntimeError(f"读取技能文件失败（{rel}）：{e}") from None
    return files


def build_migration_package(home: Path, *, enforce_limit: bool = True) -> tuple[bytes, dict]:
    """打整包：会话库一致性快照 + 技能目录 + ui.json + 脱敏 config.toml。

    返回 (zip_bytes, manifest)。manifest 含各内容文件的名称与大小（导入侧
    逐项比对用）、技能清单、被剥掉的配置键、引擎与包格式版本。会话库用
    SQLite backup API 拷一致性快照（并发写进行中拷出的也是完整库）；其余
    文件是引擎自有状态文件，textio 原子写保证读到的是完整内容。整包超过
    MAX_MIGRATION_PACKAGE_BYTES 抛可读错误（v1 尺寸上限，防 WS 帧爆炸）。
    """
    home = Path(home)
    files: list[tuple[str, bytes]] = []

    # ---- 会话库：backup API 一致性快照，不直接拷使用中的库文件 ----
    db_src = home / "skysheep.db"
    if db_src.is_file() and db_src.stat().st_size > 0:
        with tempfile.TemporaryDirectory(prefix="skysheep-migration-") as td:
            snap = Path(td) / "skysheep.db"
            _snapshot_or_copy(db_src, snap)
            files.append(("skysheep.db", snap.read_bytes()))

    # ---- config.toml：脱敏（密钥类键整行删除），脱完必须仍是合法 TOML ----
    cfg = home / "config.toml"
    sanitized_keys: list[str] = []
    if cfg.is_file():
        loaded = read_text_file(cfg)
        sanitized, sanitized_keys = sanitize_config_text(loaded.text)
        try:
            tomllib.loads(sanitized)
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(
                f"config.toml 脱敏后无法解析（{e}），已取消导出——"
                "请检查该文件的手改部分后重试"
            ) from None
        files.append(("config.toml", sanitized.encode("utf-8")))

    # ---- ui.json：引擎自有状态文件，原样进包（导入侧做 accept_edits 剥离）----
    ui = home / "ui.json"
    if ui.is_file():
        blob = ui.read_bytes()
        try:
            json.loads(blob.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise RuntimeError(f"ui.json 不是合法 JSON（{e}），已取消导出") from None
        files.append(("ui.json", blob))

    # ---- 技能目录 ----
    files.extend(_list_skill_files(home / "skills"))

    # ---- manifest（不含自身）：导入侧的清单比对依据 ----
    manifest = {
        "kind": "skysheep-migration",
        "format_version": MIGRATION_FORMAT_VERSION,
        "created": datetime.now().isoformat(timespec="seconds"),
        "engine_version": __version__,
        "files": [{"name": name, "size": len(blob)} for name, blob in files],
        "skills": sorted({n.split("/")[1] for n, _ in files if n.startswith("skills/")}),
        "sanitized_keys": sanitized_keys,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in files:
            zf.writestr(name, blob)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    data = buf.getvalue()
    if enforce_limit and len(data) > MAX_MIGRATION_PACKAGE_BYTES:
        # 调用方（migration.export）默认不走上限：超了改落盘模式，不再直接拒绝
        raise RuntimeError(
            f"迁移包 {len(data) / 1024 / 1024:.1f}MB 超过 "
            f"{MAX_MIGRATION_PACKAGE_BYTES // 1024 // 1024}MB 上限"
        )
    return data, manifest


def _prune_backups_dir(backup_dir: Path, stem: str) -> None:
    """导入备份目录按 BACKUP_KEEP 滚动裁剪（与 store._prune_backups 同款）。"""
    backups = sorted(backup_dir.glob(stem + "-*.db"))
    for old in backups[: max(0, len(backups) - SessionStore.BACKUP_KEEP)]:
        try:
            old.unlink()
        except OSError:
            pass


def apply_migration_package(
    home: Path, data: bytes, *, backup_dir: Path,
    skills_mode: str = "merge", max_bytes: int | None = MAX_MIGRATION_PACKAGE_BYTES,
) -> dict:
    """应用迁移包：白名单校验 → 降防护清洗 → 备份本机库 → 原子替换 / 合并 / 还原。

    全部校验（白名单、技能内容预算、manifest 比对、TOML/JSON、库 quick_check
    与版本）都在任何写盘之前完成——坏包根本不落盘。包内库在换上本机前按
    「降防护状态」清洗（自动化 / 白名单 / 渠道绑定表整表清空），config.toml
    的防护总开关键与 ui.json 的 accept_edits 一律剥掉。写入阶段失败的回滚是
    尽力而为：config/ui 按写前字节还原、新装技能目录删除、被覆盖的同名技能
    按备份还原、库按导入前备份还原，还原不了的名字如实带在错误信息里。
    调用方（MigrationMixin）负责先收掉本进程攥着的库连接、调用后重开。

    返回 manifest：导入了什么、备份在哪、密钥需重填提示（sanitized_keys）、
    清洗披露（db_cleaned：表名 → 清掉的行数；config_guard_keys_stripped：
    被剥掉的防护开关键）。
    """
    home = Path(home)
    backup_dir = Path(backup_dir)
    if not data:
        raise RuntimeError("迁移包内容为空")
    if skills_mode not in SKILLS_MODES:
        raise RuntimeError(f"未知的技能还原口径：{skills_mode}（只能是 merge / replace）")
    if max_bytes is not None and len(data) > max_bytes:
        raise RuntimeError(
            f"迁移包超过尺寸上限（{max_bytes // (1024 * 1024)}MB），已拒绝导入"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"迁移包不是合法 zip：{e}") from None

    with zf:
        # ---- 1) 条目白名单：未知/穿越/盘符一律整体拒绝（不做静默跳过）----
        # skills 条目同时累计「条目数 / 声明解压大小」，预算与技能安装器同一套
        # （MAX_SKILL_FILES / MAX_UNPACKED_BYTES）。大小按 zip 中央目录里声明的
        # 解压后尺寸计——zipfile 解压时超出声明尺寸会直接报错，所以这是真实
        # 解压预算；扫描阶段就拒绝 zip bomb，此时一个字节都还没解压落盘。
        entries: dict[str, zipfile.ZipInfo] = {}
        skill_entry_count = 0
        skill_entry_bytes = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = name.split("/")
            if not _entry_parts_clean(parts):
                raise RuntimeError(
                    f"迁移包里含不安全的条目路径（路径穿越/盘符），已整体拒绝：{info.filename}"
                )
            if name in _TOP_ENTRIES:
                entries[name] = info
            elif parts[0] == "skills" and len(parts) >= 3 and _skill_name_ok(parts[1], home / "skills"):
                skill_entry_count += 1
                skill_entry_bytes += max(0, int(info.file_size))
                if skill_entry_count > MAX_SKILL_FILES:
                    raise RuntimeError(
                        f"迁移包里的技能文件数过多（超过 {MAX_SKILL_FILES} 个），已整体拒绝"
                    )
                if skill_entry_bytes > MAX_UNPACKED_BYTES:
                    raise RuntimeError(
                        "迁移包里的技能内容过大"
                        f"（解压累计超过 {MAX_UNPACKED_BYTES // (1024 * 1024)}MB），已整体拒绝"
                    )
                entries[name] = info
            else:
                raise RuntimeError(f"迁移包里含未知条目，已整体拒绝：{name}")

        # ---- 2) manifest 比对：内容文件名与大小逐项对上 ----
        if "manifest.json" not in entries:
            raise RuntimeError("迁移包缺少 manifest.json（不是本版本生成的迁移包）")
        try:
            pkg_manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise RuntimeError(f"迁移包的 manifest.json 解析失败：{e}") from None
        if not isinstance(pkg_manifest, dict):
            raise RuntimeError("迁移包的 manifest.json 内容不是 JSON 对象")
        fmt = pkg_manifest.get("format_version")
        if not isinstance(fmt, int) or fmt > MIGRATION_FORMAT_VERSION:
            raise RuntimeError(
                f"迁移包格式版本更新（{fmt} > {MIGRATION_FORMAT_VERSION}），"
                "请先升级本机 SkySheep 再导入"
            )
        declared = {
            str(f.get("name")): f.get("size")
            for f in pkg_manifest.get("files", []) if isinstance(f, dict)
        }
        actual = {
            name: info.file_size for name, info in entries.items()
            if name != "manifest.json"
        }
        if declared != actual:
            raise RuntimeError(
                "迁移包内容与 manifest 清单不符（包可能被截断或篡改），已拒绝导入"
            )
        if not actual:
            raise RuntimeError("迁移包里没有可导入的内容")

        # ---- 3) 内容校验与降防护清洗（全在临时目录里进行，此时一个字节都还没写本机）----
        cleaned_tables: dict[str, int] = {}
        guard_stripped: list[str] = []
        with tempfile.TemporaryDirectory(prefix="skysheep-migration-") as td:
            stage = Path(td)
            staged_db: Path | None = None
            if "skysheep.db" in entries:
                staged_db = stage / "skysheep.db"
                with zf.open(entries["skysheep.db"]) as src, open(staged_db, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                problem = _validate_db_file(staged_db)
                if problem is not None:
                    raise RuntimeError(
                        f"迁移包里的会话库校验未通过（{problem}），已取消导入"
                    )
                conn = sqlite3.connect(str(staged_db))
                try:
                    ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    if ver > SCHEMA_VERSION:
                        raise RuntimeError(
                            f"迁移包的会话库由更新版本的 SkySheep 创建（库版本 {ver} > "
                            f"当前程序支持 {SCHEMA_VERSION}）。请先升级本机 SkySheep 再导入。"
                        )
                    # 包里库的表清单（旧库可能缺新表，缺哪张就跳过哪张）
                    have = {
                        str(r[0])
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    # 降防护状态清洗：自动化 / 白名单 / 渠道绑定表整表清空。
                    # 必须赶在换库之前做——导入完成后在跑的 cron / 流水线循环
                    # 下一轮扫描就可能认领 due / running 行，植入任务等不到
                    # 用户看完提示。表名进不了 sqlite 的参数绑定，七条清空
                    # 语句干脆逐一写成字面常量，不给任何拼接留口子；旧库
                    # 缺表就跳过（没有可清的）。清掉的行数进披露字段。
                    cleaned = 0
                    if "cron_tasks" in have:
                        cleaned = conn.execute("DELETE FROM cron_tasks").rowcount
                        if cleaned > 0:
                            cleaned_tables["cron_tasks"] = int(cleaned)
                    if "schedules" in have:
                        cleaned = conn.execute("DELETE FROM schedules").rowcount
                        if cleaned > 0:
                            cleaned_tables["schedules"] = int(cleaned)
                    if "whitelist_rules" in have:
                        cleaned = conn.execute("DELETE FROM whitelist_rules").rowcount
                        if cleaned > 0:
                            cleaned_tables["whitelist_rules"] = int(cleaned)
                    if "channel_bindings" in have:
                        cleaned = conn.execute("DELETE FROM channel_bindings").rowcount
                        if cleaned > 0:
                            cleaned_tables["channel_bindings"] = int(cleaned)
                    if "channel_sources" in have:
                        cleaned = conn.execute("DELETE FROM channel_sources").rowcount
                        if cleaned > 0:
                            cleaned_tables["channel_sources"] = int(cleaned)
                    if "pipelines" in have:
                        cleaned = conn.execute("DELETE FROM pipelines").rowcount
                        if cleaned > 0:
                            cleaned_tables["pipelines"] = int(cleaned)
                    if "pipeline_nodes" in have:
                        cleaned = conn.execute("DELETE FROM pipeline_nodes").rowcount
                        if cleaned > 0:
                            cleaned_tables["pipeline_nodes"] = int(cleaned)
                    conn.commit()
                finally:
                    conn.close()
            staged_cfg: bytes | None = None
            if "config.toml" in entries:
                try:
                    text = zf.read("config.toml").decode("utf-8")
                except UnicodeDecodeError as e:
                    raise RuntimeError(
                        f"迁移包里的 config.toml 不是 UTF-8 文本：{e}"
                    ) from None
                # 密钥红线在「入口」再兜一遍：导出方的脱敏是自觉，导入方不信任
                # 包内容——再剥一次，按脱敏后的文本写盘。
                sanitized, import_stripped = sanitize_config_text(text)
                # 防护总开关不随包生效：包里带来的 computer_control /
                # browser_control / browser_ignore_cert 一律剥掉（与 ui.json 的
                # accept_edits 同款语义），这些降防护档位只能由本机用户主动切换。
                sanitized, guard_stripped = _strip_guard_switch_keys(sanitized)
                try:
                    parsed_cfg = tomllib.loads(sanitized)
                except tomllib.TOMLDecodeError as e:
                    raise RuntimeError(
                        f"迁移包里的 config.toml 不是合法 TOML，已取消导入：{e}"
                    ) from None
                # 防护开关的表/变体形态 fail-closed 兜底：键值行剥除后，解析
                # 结构里若仍带出这三个键（[computer_control] 段、[[...]] 数组表、
                # 带引号的键等逐行手段剥不掉的形状），说明包在用表形态硬塞开关
                # ——加载侧 bool(非空表) 即真，等价于打开开关，整体拒绝导入。
                guard_residue = [k for k in _GUARD_SWITCH_KEYS if k in parsed_cfg]
                if guard_residue:
                    raise RuntimeError(
                        "迁移包里的 config.toml 含防护开关键的表/变体形态"
                        f"（{'、'.join(guard_residue)}），已整体拒绝导入"
                    )
                staged_cfg = sanitized.encode("utf-8")
                if import_stripped:
                    logger.info("迁移导入再剥密钥键：%s", "、".join(import_stripped))
                if guard_stripped:
                    logger.info("迁移导入剥防护开关键：%s", "、".join(guard_stripped))
            staged_ui: dict | None = None
            if "ui.json" in entries:
                try:
                    parsed = json.loads(zf.read("ui.json").decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as e:
                    raise RuntimeError(
                        f"迁移包里的 ui.json 不是合法 JSON，已取消导入：{e}"
                    ) from None
                if not isinstance(parsed, dict):
                    raise RuntimeError("迁移包里的 ui.json 内容不是 JSON 对象，已取消导入")
                staged_ui = parsed
            staged_skills = stage / "skills"
            for name in sorted(entries):
                if not name.startswith("skills/"):
                    continue
                target = stage / name
                if not target.resolve().is_relative_to(stage.resolve()):
                    raise RuntimeError(f"迁移包条目路径逃逸出暂存区，已拒绝：{name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entries[name]) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

            # ---- 4) 备份本机库（_snapshot_or_copy 一致性快照；坏库退回裸拷贝留底）----
            main_db = home / "skysheep.db"
            backup_path: Path | None = None
            had_db = main_db.is_file() and main_db.stat().st_size > 0
            if had_db:
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                backup_path = backup_dir / f"{main_db.stem}-{stamp}{_PRE_IMPORT_TAG}.db"
                _snapshot_or_copy(main_db, backup_path)
                _prune_backups_dir(backup_dir, main_db.stem)

            # ---- 5) 写入（尽力回滚；attempted 记下真正动过的文件，回滚不越界）----
            cfg_old: bytes | None = None
            ui_old: bytes | None = None
            attempted: set[str] = set()
            skills_root = home / "skills"
            skills_new: list[str] = []
            skills_overwritten: list[str] = []
            skills_moved: list[tuple[str, Path]] = []  # 被覆盖技能的备份位（回滚用，两档通用）
            db_replaced = False
            try:
                # 5a) 库：清 WAL/SHM 残留 → _stage_and_replace（同目录临时文件 +
                # quick_check + os.replace 原子换上；校验不过主库未动）
                if staged_db is not None:
                    attempted.add("skysheep.db")
                    for suffix in ("-wal", "-shm"):
                        try:
                            Path(str(main_db) + suffix).unlink()
                        except OSError:
                            pass
                    _stage_and_replace(staged_db, main_db)
                    db_replaced = True
                # 5b) config.toml / ui.json：textio 原子写（写前字节留快照）
                if staged_cfg is not None:
                    attempted.add("config.toml")
                    cfg_path = home / "config.toml"
                    try:
                        cfg_old = cfg_path.read_bytes()
                    except OSError:
                        cfg_old = None
                    write_bytes_atomic(cfg_path, staged_cfg)
                if staged_ui is not None:
                    attempted.add("ui.json")
                    ui_path = home / "ui.json"
                    try:
                        ui_old = ui_path.read_bytes()
                    except OSError:
                        ui_old = None
                    # 「自动允许写入」是降防护档位，只能本机用户主动切换：
                    # 包里带来的该字段一律剥掉（与 settings.import 同一防护）
                    staged_ui.pop("accept_edits", None)
                    write_text_atomic(
                        ui_path, json.dumps(staged_ui, ensure_ascii=False),
                        encoding="utf-8",
                    )
                # 5c) 技能还原：同名覆盖，本机多出的保留；目录名先过安装器同款
                # 校验并 resolve 确认仍在技能根内。
                # 被覆盖的同名技能先留还原路：replace 档（P24 v2）整目录移进
                # 备份区，避免「包里旧文件 + 本机新文件」混成一份谁也说不清的
                # 状态；merge 档整份复制留底（本机目录原地不动，只叠加包内
                # 文件，本机多出的文件不丢）。两档都没有备份就拒绝覆盖——
                # 绝不裸改本机技能；备份区里的目录在写盘失败时按 moved 清单
                # 原样搬回（_rollback_import 同一条通道）。
                if staged_skills.is_dir():
                    skills_root.mkdir(parents=True, exist_ok=True)
                    staged_dirs: list[tuple[Path, Path, str]] = []
                    for src_dir in sorted(
                        p for p in staged_skills.iterdir() if p.is_dir()
                    ):
                        try:
                            dst = _skill_target(skills_root, src_dir.name)
                        except SkillInstallError as e:
                            raise RuntimeError(
                                f"包里的技能名不合法（{src_dir.name}）：{e}"
                            ) from None
                        staged_dirs.append((src_dir, dst, src_dir.name))
                    aside_root = backup_dir / ("skills-" + time.strftime("%Y%m%d-%H%M%S"))
                    for _src, dst, name in staged_dirs:
                        if not dst.is_dir():
                            if skills_mode != "replace":
                                skills_new.append(name)
                            continue
                        aside = aside_root / name
                        aside.parent.mkdir(parents=True, exist_ok=True)
                        if aside.exists():
                            shutil.rmtree(aside, ignore_errors=True)
                        if skills_mode == "replace":
                            shutil.move(str(dst), str(aside))
                        else:
                            shutil.copytree(dst, aside)
                        skills_moved.append((name, aside))
                        skills_overwritten.append(name)
                    for src_dir, dst, _name in staged_dirs:
                        shutil.copytree(src_dir, dst, dirs_exist_ok=True)
            except Exception as e:  # noqa: BLE001
                residual = _rollback_import(
                    home, main_db, backup_path, had_db, db_replaced,
                    cfg_old, ui_old, skills_root, skills_new, attempted,
                    skills_moved,
                )
                extra = f"（残留未还原：{'、'.join(residual)}）" if residual else ""
                raise RuntimeError(f"迁移导入失败，已尽力回滚本机数据{extra}：{e}") from None

        return {
            "format_version": MIGRATION_FORMAT_VERSION,
            "created": pkg_manifest.get("created", ""),
            "engine_version": pkg_manifest.get("engine_version", ""),
            "db_restored": staged_db is not None,
            "config_restored": staged_cfg is not None,
            "ui_restored": staged_ui is not None,
            "skills_new": sorted(skills_new),
            "skills_overwritten": sorted(skills_overwritten),
            "skills_count": len(skills_new) + len(skills_overwritten),
            "backup": str(backup_path) if backup_path else "",
            "backup_dir": str(backup_dir),
            "sanitized_keys": sorted(
                {str(k) for k in pkg_manifest.get("sanitized_keys", [])}
            ),
            # 清洗披露：随包换上的库清掉了哪些自动化/绑定/白名单表（表 → 行数）、
            # config 里剥掉了哪些防护总开关键。前端导入完成提示逐项展示。
            "db_cleaned": dict(sorted(cleaned_tables.items())),
            "config_guard_keys_stripped": sorted(set(guard_stripped)),
        }


def _rollback_import(
    home: Path,
    main_db: Path,
    backup_path: Path | None,
    had_db: bool,
    db_replaced: bool,
    cfg_old: bytes | None,
    ui_old: bytes | None,
    skills_root: Path,
    skills_new: list[str],
    attempted: set[str],
    skills_moved: list[tuple[str, Path]] | None = None,
) -> list[str]:
    """写入阶段失败的尽力回滚；返回还原不了的名字（调用方如实上报）。

    只碰 attempted 里的文件：包里没有的条目（attempted 不含）即使快照是
    None 也不 unlink——回滚绝不越过本次导入真正动过的范围。
    """
    failed: list[str] = []
    if db_replaced:
        try:
            if backup_path is not None and backup_path.is_file():
                _stage_and_replace(backup_path, main_db)
            elif not had_db:
                main_db.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            failed.append("skysheep.db")
            logger.warning("迁移导入回滚会话库失败", exc_info=True)
    for name, old in (("config.toml", cfg_old), ("ui.json", ui_old)):
        if name not in attempted:
            continue
        path = home / name
        if old is None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failed.append(name)
        else:
            try:
                write_bytes_atomic(path, old)
            except OSError:
                failed.append(name)
    for name in skills_new:
        try:
            shutil.rmtree(skills_root / name, ignore_errors=True)
        except OSError:
            failed.append("skills/" + name)
    # 被覆盖的同名技能（replace 档移走的、merge 档复制留底的都在这条清单里）：
    # 先清掉这次拷进来的，再把备份位里的本机原目录搬回原位；搬不回的逐个
    # 点名（调用方把 residual 带进报错）。
    for name, aside in (skills_moved or []):
        try:
            shutil.rmtree(skills_root / name, ignore_errors=True)
            if aside.is_dir():
                shutil.move(str(aside), str(skills_root / name))
        except OSError:
            failed.append("skills/" + name)
    return failed


class MigrationMixin:
    """跨机全量迁移：整包导出（base64 回传）与导入（替换本机数据，仅本机）。"""

    async def migration_export(self) -> dict:
        """整包导出：会话库快照 + 技能 + ui.json + 脱敏 config.toml。

        打包放线程跑（库快照与技能收集是阻塞 I/O）；导出是纯读面——库走
        backup API 一致性快照、其余是引擎原子写的状态文件，不需要忙碌保护。
        密钥不进包（mcp.json 整个排除、config.toml 剥密钥类键）。

        **两种回传形态（P24 v2）**：包体 ≤64MB 时按 base64 经 WS 回传（`mode=inline`，
        前端直接触发下载）；超过上限不拒绝，改写到 `~/.skysheep/migrations/` 并回传
        路径（`mode=file`）。注意：导入侧与两种回传来源同受 64MB 上限（防 zip
        bomb），所以超过上限的落盘包**无法再经导入换回本机**，只作拷贝存档用
        ——「大库跨机迁移」不再是受支持的能力。落盘目录按 MIGRATION_PACKAGE_KEEP
        滚动裁剪。
        """
        data, manifest = await asyncio.to_thread(
            build_migration_package, skysheep_home(), enforce_limit=False
        )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"skysheep-migration-{stamp}.zip"
        if len(data) <= MAX_MIGRATION_PACKAGE_BYTES:
            return {
                "mode": "inline",
                "filename": filename,
                "data_b64": base64.b64encode(data).decode("ascii"),
                "manifest": manifest,
                "size": len(data),
                "limit_mb": MAX_MIGRATION_PACKAGE_BYTES // 1024 // 1024,
            }
        out_dir = skysheep_home() / "migrations"
        path = out_dir / filename
        await asyncio.to_thread(_write_package_file, out_dir, path, data)
        return {
            "mode": "file",
            "filename": filename,
            "path": str(path),
            "manifest": manifest,
            "size": len(data),
            "limit_mb": MAX_MIGRATION_PACKAGE_BYTES // 1024 // 1024,
        }

    async def migration_import(self, params: dict) -> dict:
        """整包导入：替换本机数据（库 / 技能 / ui.json / 脱敏 config.toml）。

        危险操作的三道闸：会话/定时/流水线在飞走 ``_backup_busy_message``
        拒绝；团队自动循环在飞（``_teams`` 任一 ``loop_running``）拒绝；
        包校验全部通过且本机库已备份后才动盘。改了库必须重启生效
        （restart_required=True，前端据此提示）。

        包来源两种（P24 v2）：`data_b64`（界面选择的包）或 `path`（本机磁盘上的
        包）。两种来源受同一道尺寸上限（``MAX_MIGRATION_PACKAGE_BYTES``，防
        zip bomb 写满磁盘）与同一套技能内容解压预算——磁盘上的恶意包不因
        「本机操作」豁免。`skills_mode` 决定同名技能是叠加覆盖（merge，默认）
        还是整目录替换（replace）。
        """
        busy = self._backup_busy_message("导入")
        if busy:
            raise RuntimeError(busy)
        flying = [sid for sid, t in self._teams.items()
                  if getattr(t, "loop_running", False)]
        if flying:
            raise RuntimeError(
                f"有 {len(flying)} 支团队的自动循环正在运行，先收队（或等交付）再导入"
            )
        skills_mode = str(params.get("skills_mode") or "merge").strip().lower()
        if skills_mode not in SKILLS_MODES:
            raise RuntimeError("技能还原口径只能是 merge（叠加）或 replace（替换）")
        path_arg = str(params.get("path") or "").strip()
        max_bytes = MAX_MIGRATION_PACKAGE_BYTES  # 两种来源同一道上限（调用时读取，测试可调）
        if path_arg:
            pkg_path = Path(path_arg)
            if not pkg_path.is_file():
                raise RuntimeError(f"找不到迁移包文件：{path_arg}")
            data = await asyncio.to_thread(pkg_path.read_bytes)
        else:
            raw = str(params.get("data_b64") or "")
            if not raw:
                raise RuntimeError("缺少迁移包内容（data_b64 或 path）")
            try:
                data = base64.b64decode(raw, validate=True)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"迁移包内容不是合法的 base64：{e}") from None
        home = skysheep_home()
        store = self.store
        backup_dir = store.backup_dir() if store is not None else home / "backups"
        # 换库文件前收掉本进程攥着的库连接（与 restore_backup / _replace_store_db
        # 同款：常驻写连接与 aiosqlite 连接都攥着文件句柄，不收 os.replace 会被拒）
        had_store = store is not None
        if had_store:
            if store._db is not None:
                await store._db.close()
                store._db = None
            await store._close_writer()
        try:
            manifest = await asyncio.to_thread(
                apply_migration_package, home, data, backup_dir=backup_dir,
                skills_mode=skills_mode, max_bytes=max_bytes,
            )
        finally:
            if had_store:
                await store.connect()
        manifest["skills_mode"] = skills_mode
        return {"manifest": manifest, "restart_required": True}


def _write_package_file(out_dir: Path, path: Path, data: bytes) -> None:
    """把超大包写到落盘目录（同目录临时文件 + 原子换名），并滚动裁剪旧包。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    # 临时名掺随机分量：文件名的时间戳只到秒，同秒并发导出（或两次手动落盘）
    # 会算出同一个确定性的 .zip.tmp 名，两次写盘交替进行时产物可能互相混写。
    # 随机后缀让每个写入者各用各的临时文件，最后 os.replace 谁后完成谁生效，
    # 落盘的永远是一份完整包（与 skills/installer.py 的暂存目录同一思路）。
    tmp = path.with_name(f"{path.stem}-{os.urandom(4).hex()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # 换名成功后不存在，no-op；失败则清掉残片
    olds = sorted(out_dir.glob("skysheep-migration-*.zip"),
                  key=lambda p: p.stat().st_mtime)
    for old in olds[: max(0, len(olds) - MIGRATION_PACKAGE_KEEP)]:
        try:
            old.unlink()
        except OSError:
            pass
