"""SessionStore 的数据库快照、滚动备份、列表与备份恢复逻辑。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from ._store_types import _ALL  # noqa: F401 - 供同族模块对齐引用

logger = logging.getLogger("skysheep.store")


def _store_strftime(fmt: str) -> str:
    """读取 session.store 模块命名空间的 time.strftime，兼容测试对其 monkeypatch。"""
    mod = sys.modules.get("skysheep.session.store")
    tmod = getattr(mod, "time", time) if mod is not None else time
    return tmod.strftime(fmt)


def parse_backup_stamp(stamp: str) -> float | None:
    """把备份文件名里的时间戳（`%Y%m%d-%H%M%S`，后面可能跟「-恢复前」）解析成时刻。"""
    try:
        return time.mktime(time.strptime(stamp[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


def _snapshot_db(src: Path, target: Path) -> None:
    """用 SQLite backup API 把库拷成一份一致性快照（同步函数，调用方放线程跑）。"""
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(target))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _snapshot_or_copy(src: Path, target: Path) -> None:
    """一致性快照；源库打不开（本身已损坏）时退回裸拷贝留底。"""
    try:
        _snapshot_db(src, target)
    except (sqlite3.Error, OSError):
        shutil.copy2(src, target)


def _validate_db_file(path: Path) -> str | None:
    """打开试读 + PRAGMA quick_check。返回 None 表示可用，否则给可读原因。"""
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as e:
        return f"无法打开（{e}）"
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as e:
        return f"读取失败（{e}）"
    finally:
        conn.close()
    verdict = str(row[0]) if row else ""
    if verdict == "ok":
        return None
    return "quick_check 未通过：" + (verdict or "空结果")


def _stage_and_replace(src: Path, main: Path) -> None:
    """把备份拷到主库同目录临时文件，quick_check 校验通过才原子换上主库。"""
    fd, tmp_name = tempfile.mkstemp(dir=str(main.parent), prefix=main.name + ".", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        problem = _validate_db_file(tmp)
        if problem is not None:
            raise ValueError(
                f"备份文件校验未通过（疑似损坏），当前数据库未改动：{problem}"
            )
        os.replace(tmp, main)
    except BaseException:
        for p in (tmp, Path(tmp_name + "-wal"), Path(tmp_name + "-shm")):
            try:
                p.unlink()
            except OSError:
                pass
        raise


class BackupMixin:
    async def _rolling_backup(self, existed: bool = True) -> str | None:
        """连接建立后做滚动备份（保留最近 BACKUP_KEEP 份），防止误删无法恢复。"""
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
            if self.path.stat().st_size <= 0:
                return None
            stamp = _store_strftime("%Y%m%d-%H%M%S")
            target = backup_dir / f"{self.path.stem}-{stamp}.db"

            def _copy() -> bool:
                try:
                    if not target.exists():
                        _snapshot_db(self.path, target)
                    return True
                except (OSError, sqlite3.Error):
                    try:
                        target.unlink()
                    except OSError:
                        pass
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

    def backup_dir(self) -> Path:
        return self.path.parent / "backups"

    async def backup_now(self) -> dict:
        """手动触发一次备份（设置 · 关于的「立即备份」按钮）。"""
        if not self.path.exists() or self.path.stat().st_size <= 0:
            raise RuntimeError("还没有可备份的会话数据")
        d = self.backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{self.path.stem}-{_store_strftime('%Y%m%d-%H%M%S')}.db"

        def _produce() -> None:
            try:
                if target.exists():
                    target.unlink()
                _snapshot_db(self.path, target)
            except (OSError, sqlite3.Error) as e:
                try:
                    target.unlink()
                except OSError:
                    pass
                raise RuntimeError(f"备份失败：{e}") from e

        await asyncio.to_thread(_produce)
        self._prune_backups(sorted(d.glob(self.path.stem + "-*.db")))
        return {"name": target.name, "path": str(target)}

    async def delete_backup(self, name: str) -> dict:
        """删除单份备份（设置 · 关于的备份列表，每行一个删除入口）。"""
        d = self.backup_dir()
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
        """可用备份列表（新的在前）：备份时刻、大小、路径。"""
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
                "taken": cur.st_mtime,
                "stamp": "当前",
                "safety": False,
                "current": True,
            })
        except OSError:
            pass
        items.sort(key=lambda x: (not x["current"], -x["taken"]))
        return items

    async def restore_backup(self, name: str) -> dict:
        """用某个备份覆盖当前会话库（重启前先给现状留一份备份，可再换回来）。"""
        d = self.backup_dir()
        if Path(name).name != name or not name.endswith(".db"):
            raise ValueError("备份文件名不合法")
        src = (d / name).resolve()
        try:
            src.relative_to(d.resolve())
        except ValueError:
            raise ValueError("备份文件名不合法") from None
        if not src.is_file():
            raise FileNotFoundError("备份不存在：" + name)
        safety = None
        if self.path.exists() and self.path.stat().st_size > 0:
            stamp = _store_strftime("%Y%m%d-%H%M%S")
            safety = d / f"{self.path.stem}-{stamp}{self.SAFETY_TAG}.db"
            try:
                await asyncio.to_thread(_snapshot_or_copy, self.path, safety)
            except (OSError, sqlite3.Error):
                safety = None
        async with self._writer_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None
            if self._writer is not None:
                try:
                    await self._writer.close()
                except Exception:  # noqa: BLE001
                    logger.warning("关闭独立写连接失败（忽略）：%s", self.path.name)
                self._writer = None
                self._writer_owner = None
            for suffix in ("-wal", "-shm"):
                try:
                    Path(str(self.path) + suffix).unlink()
                except OSError:
                    pass
            try:
                await asyncio.to_thread(_stage_and_replace, src, self.path)
            except Exception:
                try:
                    await self.connect()
                except Exception:  # noqa: BLE001
                    pass
                raise
            await self.connect()
        return {
            "restored": src.name,
            "safety_copy": str(safety) if safety else "",
        }
