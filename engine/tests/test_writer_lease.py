"""跨进程写者租约（审计 L26）：租约原语 + 备份预检的跨进程可见性。

会话库备份/恢复只能看见本进程的在跑轮次；schtasks 拉起的 cron-run 子进程、
命令行会话、headless run 都是别的进程，它们整段持写者租约，备份端探测后拒绝。
"""

from __future__ import annotations

from skysheep.session.writer_lease import WriterLease, other_writer_active


def test_lease_is_exclusive_and_releases(tmp_path):
    path = tmp_path / "db-writer.lock"
    first = WriterLease(path)
    assert first.acquire() is True
    # 同一进程的第二个句柄也拿不到：锁是 OS 级、按文件句柄/打开实例算
    second = WriterLease(path)
    assert second.acquire() is False
    assert other_writer_active(path) is True
    first.release()
    assert other_writer_active(path) is False
    assert second.acquire() is True
    second.release()


def test_lease_context_manager_releases_on_error(tmp_path):
    path = tmp_path / "db-writer.lock"
    try:
        with WriterLease(path):
            assert other_writer_active(path) is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert other_writer_active(path) is False, "异常退出也必须放锁"


def test_acquire_is_idempotent_for_same_instance(tmp_path):
    path = tmp_path / "db-writer.lock"
    lease = WriterLease(path)
    assert lease.acquire() is True
    assert lease.acquire() is True  # 重复 acquire 不换句柄、不自锁
    lease.release()
    lease.release()  # 重复释放无害
    assert lease.held is False


def test_backup_precheck_refuses_when_other_process_writes(home):
    """备份预检：别的进程握着写者租约时给出可读原因，而不是硬着头皮替换库。"""
    from skysheep.config import skysheep_home
    from skysheep.server.backend_parts.lifecycle import LifecycleMixin

    class _Backend(LifecycleMixin):
        _base_run_task = None
        runtimes: dict = {}
        _cron_running = False
        _pipeline_running = False

    be = _Backend()
    assert be._backup_busy_message("恢复") is None

    lease = WriterLease(skysheep_home() / "db-writer.lock")
    assert lease.acquire() is True
    try:
        msg = be._backup_busy_message("恢复")
        assert msg and "另一个 SkySheep 进程" in msg
        # 探测不持有锁：反复调用结果稳定，且不会把租约抢走
        assert be._backup_busy_message("备份") is not None
    finally:
        lease.release()
    assert be._backup_busy_message("恢复") is None
