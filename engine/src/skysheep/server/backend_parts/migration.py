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
- 导入前必须先备份本机库：复用 store 的 ``_snapshot_or_copy``（SQLite
  backup API 一致性快照，坏库退回裸拷贝留底），落 store 的 backups 目录、
  按 ``-迁移前`` 后缀标注，BACKUP_KEEP 滚动裁剪照旧；
- 库文件替换走 ``_stage_and_replace``（同目录临时文件 + quick_check +
  os.replace 原子换上）；config.toml / ui.json 走 textio 原子写；
- 技能目录**合并**还原（同名覆盖、本机多出的技能保留不删）——迁移不是
  「克隆」，清空本机技能目录的风险大于残留旧技能；
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
from ...skills.installer import SkillInstallError, _skill_target
from ...textio import read_text_file, write_bytes_atomic, write_text_atomic

logger = logging.getLogger(__name__)

# 包格式版本：将来加新条目类型时 +1，旧引擎收到更高版本的包直接拒
# （与库版本降级防护同一姿态：新版产物不能被旧程序硬吃）。
MIGRATION_FORMAT_VERSION = 1

# 整包尺寸上限（v1）：包体 base64 后经 WS 传输，64MB 是防帧爆炸的经验上限。
# 超过时给可读错误，指路用 data.export_full（落盘导出，不走 WS）。
MAX_MIGRATION_PACKAGE_BYTES = 64 * 1024 * 1024

# config.toml 脱敏的键名标记（小写匹配、含即删行）：与任务口径一致的四类。
# 注意比 support.SECRET_KEYS 窄（那边还有 key/passwd/credential 等）——迁移
# 包要在另一台机器上「开箱可用」，只剥明确的密钥字段，避免误伤普通配置。
_SECRET_KEY_MARKERS = ("api_key", "secret", "token", "password")

# 包内顶层固定条目（白名单的精确名部分）
_TOP_ENTRIES = ("skysheep.db", "config.toml", "ui.json", "manifest.json")

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


def build_migration_package(home: Path) -> tuple[bytes, dict]:
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
    if len(data) > MAX_MIGRATION_PACKAGE_BYTES:
        raise RuntimeError(
            f"迁移包 {len(data) / 1024 / 1024:.1f}MB 超过 "
            f"{MAX_MIGRATION_PACKAGE_BYTES // 1024 // 1024}MB 上限（经界面传输的包体"
            "不宜过大）；请改用「设置 → 关于」的完整导出（落盘文件，不经界面传输）"
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


def apply_migration_package(home: Path, data: bytes, *, backup_dir: Path) -> dict:
    """应用迁移包：白名单校验 → 备份本机库 → 原子替换 db / 合并技能 / 还原配置。

    全部校验（白名单、manifest 比对、TOML/JSON、库 quick_check 与版本）都在
    任何写盘之前完成——坏包根本不落盘。写入阶段失败的回滚是尽力而为：
    config/ui 按写前字节还原、新装技能目录删除、库按导入前备份还原，
    还原不了的名字如实带在错误信息里。调用方（MigrationMixin）负责先收掉
    本进程攥着的库连接、调用后重开。

    返回 manifest：导入了什么、备份在哪、密钥需重填提示（sanitized_keys）。
    """
    home = Path(home)
    backup_dir = Path(backup_dir)
    if not data:
        raise RuntimeError("迁移包内容为空")
    if len(data) > MAX_MIGRATION_PACKAGE_BYTES:
        raise RuntimeError("迁移包超过尺寸上限，已拒绝导入")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"迁移包不是合法 zip：{e}") from None

    with zf:
        # ---- 1) 条目白名单：未知/穿越/盘符一律整体拒绝（不做静默跳过）----
        entries: dict[str, zipfile.ZipInfo] = {}
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

        # ---- 3) 内容校验（全在临时目录里进行，此时一个字节都还没写本机）----
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
                finally:
                    conn.close()
                if ver > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"迁移包的会话库由更新版本的 SkySheep 创建（库版本 {ver} > "
                        f"当前程序支持 {SCHEMA_VERSION}）。请先升级本机 SkySheep 再导入。"
                    )
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
                try:
                    tomllib.loads(sanitized)
                except tomllib.TOMLDecodeError as e:
                    raise RuntimeError(
                        f"迁移包里的 config.toml 不是合法 TOML，已取消导入：{e}"
                    ) from None
                staged_cfg = sanitized.encode("utf-8")
                if import_stripped:
                    logger.info("迁移导入再剥密钥键：%s", "、".join(import_stripped))
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
                # 5c) 技能合并：同名覆盖、本机多出的保留；目录名先过安装器
                # 同款校验并 resolve 确认仍在技能根内
                if staged_skills.is_dir():
                    skills_root.mkdir(parents=True, exist_ok=True)
                    for src_dir in sorted(
                        p for p in staged_skills.iterdir() if p.is_dir()
                    ):
                        try:
                            dst = _skill_target(skills_root, src_dir.name)
                        except SkillInstallError as e:
                            raise RuntimeError(
                                f"包里的技能名不合法（{src_dir.name}）：{e}"
                            ) from None
                        if dst.is_dir():
                            skills_overwritten.append(src_dir.name)
                        else:
                            skills_new.append(src_dir.name)
                        shutil.copytree(src_dir, dst, dirs_exist_ok=True)
            except Exception as e:  # noqa: BLE001
                residual = _rollback_import(
                    home, main_db, backup_path, had_db, db_replaced,
                    cfg_old, ui_old, skills_root, skills_new, attempted,
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
    return failed


class MigrationMixin:
    """跨机全量迁移：整包导出（base64 回传）与导入（替换本机数据，仅本机）。"""

    async def migration_export(self) -> dict:
        """整包导出：会话库快照 + 技能 + ui.json + 脱敏 config.toml。

        打包放线程跑（库快照与技能收集是阻塞 I/O）；导出是纯读面——库走
        backup API 一致性快照、其余是引擎原子写的状态文件，不需要忙碌保护。
        密钥不进包（mcp.json 整个排除、config.toml 剥密钥类键）。
        """
        data, manifest = await asyncio.to_thread(
            build_migration_package, skysheep_home()
        )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return {
            "filename": f"skysheep-migration-{stamp}.zip",
            "data_b64": base64.b64encode(data).decode("ascii"),
            "manifest": manifest,
            "size": len(data),
        }

    async def migration_import(self, params: dict) -> dict:
        """整包导入：替换本机数据（库 / 技能 / ui.json / 脱敏 config.toml）。

        危险操作的三道闸：会话/定时/流水线在飞走 ``_backup_busy_message``
        拒绝；团队自动循环在飞（``_teams`` 任一 ``loop_running``）拒绝；
        包校验全部通过且本机库已备份后才动盘。改了库必须重启生效
        （restart_required=True，前端据此提示）。
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
        raw = str(params.get("data_b64") or "")
        if not raw:
            raise RuntimeError("缺少迁移包内容（data_b64）")
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
                apply_migration_package, home, data, backup_dir=backup_dir
            )
        finally:
            if had_store:
                await store.connect()
        return {"manifest": manifest, "restart_required": True}
