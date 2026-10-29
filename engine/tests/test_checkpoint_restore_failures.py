"""审查 L10 回归：restore 单文件失败不再中断整轮回滚。

旧行为：单文件 unlink 抛 OSError 会中断在半路（已恢复/未恢复混杂、无清单），
目录删除 shutil.rmtree(ignore_errors=True) 静默吞错还照样把该路径记进「已恢复」；
新行为逐文件收集 failed 继续跑，调用方拿到 {restored, failed} 的完整盘面。
"""

from __future__ import annotations

from pathlib import Path

from skysheep.core.checkpoints import CheckpointStore


def test_restore_collects_per_file_failures(tmp_path, monkeypatch):
    root = tmp_path / "cps"
    work = tmp_path / "work"
    work.mkdir()
    store = CheckpointStore(root)
    (work / "keep.txt").write_bytes(b"OLD-KEEP")
    (work / "locked.txt").write_bytes(b"LOCKED")
    cp = store.save("s1", {
        str(work / "keep.txt"): b"NEW-KEEP",   # 有改前内容 → 恢复覆盖
        str(work / "locked.txt"): None,        # 改前不存在 → 回滚应删除（将失败）
    })
    assert cp is not None

    def _raiser(self, *args, **kwargs):
        raise PermissionError("文件被另一进程占用")

    monkeypatch.setattr(Path, "unlink", _raiser)
    out = store.restore(cp["id"], force=True)

    assert out["restored"] == [str(work / "keep.txt")]
    assert len(out["failed"]) == 1
    assert out["failed"][0]["path"] == str(work / "locked.txt")
    assert "占用" in out["failed"][0]["error"]
    assert (work / "locked.txt").exists()          # 失败条目原样保留，不再虚报
    assert (work / "keep.txt").read_bytes() == b"NEW-KEEP"


def test_restore_dir_delete_failure_is_recorded(tmp_path, monkeypatch):
    root = tmp_path / "cps"
    work = tmp_path / "work"
    work.mkdir()
    store = CheckpointStore(root)
    target = work / "newdir"
    target.mkdir()
    (target / "inner.txt").write_bytes(b"X")
    cp = store.save("s1", {str(target): None})     # 改前不存在 → 回滚删目录
    assert cp is not None

    def _raiser(path, *args, **kwargs):
        raise PermissionError("目录删除被拒")

    monkeypatch.setattr("skysheep.core.checkpoints.shutil.rmtree", _raiser)
    out = store.restore(cp["id"], force=True)

    assert out["restored"] == []
    assert len(out["failed"]) == 1
    assert out["failed"][0]["path"] == str(target)
    assert target.exists()                          # 不再 ignore_errors 吞错后虚报已恢复
