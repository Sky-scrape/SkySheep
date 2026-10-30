"""跨进程写者租约（审计 L26）。

会话库备份/恢复会在「检查点 → 复制/替换」之间有一小段窗口；这段时间里如果
**另一个进程**（系统计划任务拉起的 `skysheep cron run`、命令行会话、headless
run）往旧库文件里提交，那次提交随旧 inode 一起被替换掉——进程内预检
（``_backup_busy_message``）只能看到自己的线程，看不见别的进程。

这里给「会持续写会话库的长任务」一把进程级租约：进程启动这类任务时独占
``~/.skysheep/db-writer.lock`` 的一个字节，备份/恢复动手前先探一次——别人还
握着就拒绝，让用户等它结束。锁是 OS 级的（Windows 走 ``msvcrt.locking``，
POSIX 走 ``flock``），进程崩溃时内核自动释放，不会留下需要手工清理的死锁。

设计边界（有意为之，不是遗漏）：

- **只在长任务粒度加锁**，不给每次写库加锁——每次写都抢锁会把 SQLite 自己的
  WAL 并发优势抹平，而单人桌面场景的并发写本来就极少；
- **拿不到租约不阻塞**：cron-run 等不了交互进程，探测型调用（备份预检）也不
  该等；两种情况都按「有人在写」处理，fail-closed；
- 锁文件本身不含任何业务数据，删掉也无害（下一次 acquire 会重建）。
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_NAME = "db-writer.lock"


def lease_path() -> Path:
    """租约文件路径（放在引擎数据目录，与库同盘）。"""
    from ..config import skysheep_home

    return skysheep_home() / LOCK_NAME


class WriterLease:
    """独占写者租约：``with WriterLease() as lease:`` 或 acquire/release 手动配对。

    acquire 返回 bool：False 表示**别的进程**正握着（本进程已有一把时也拿不到，
    同一进程内的多处写者应共用一个实例）。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._fd: int | None = None

    def _resolved(self) -> Path:
        return self._path or lease_path()

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        p = self._resolved()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # 解锁失败无所谓：close 时内核会释放
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> WriterLease:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    @property
    def held(self) -> bool:
        return self._fd is not None


def other_writer_active(path: Path | None = None) -> bool:
    """别的进程正握着写者租约吗？（探测一次，不持有）

    能拿到锁 = 没人写，拿到后立刻释放；拿不到 = 别人在写。
    探测本身失败（文件系统不支持锁等）按「没人在写」处理：备份/恢复的主防线
    仍是进程内预检，这里只是补一块跨进程的可见性，不该因为它误报就全禁。
    """
    probe = WriterLease(path)
    if probe.acquire():
        probe.release()
        return False
    return True
