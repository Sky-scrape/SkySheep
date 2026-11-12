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


def test_restore_prunes_created_dir_chain(tmp_path):
    """撤销本轮改动要把「本轮新建的目录链」一起收掉：write_file 会隐式建父目录
    （docs/sub/a.txt 带出 docs/ 与 docs/sub/），检查点只记文件——不清理就留下
    空目录，撤销看起来像没撤干净。"""
    root = tmp_path / "cps"
    work = tmp_path / "work"
    work.mkdir()
    keep_dir = work / "keep"
    keep_dir.mkdir()
    (keep_dir / "other.txt").write_bytes(b"KEEP")   # 非空目录：不能被顺手删掉

    deep = work / "docs" / "sub"
    deep.mkdir(parents=True)
    target = deep / "a.txt"
    target.write_bytes(b"NEW")

    store = CheckpointStore(root)
    cp = store.save("s1", {str(target): None})
    assert cp is not None

    out = store.restore(cp["id"], force=True, keep_root=work)

    assert str(target) in out["restored"]
    assert not target.exists()
    assert not (work / "docs").exists(), "本轮新建的目录链应一并收掉"
    assert work.exists(), "工作目录本身（keep_root）不能删"
    assert keep_dir.exists() and (keep_dir / "other.txt").exists()


def test_prune_created_dirs_stops_at_nonempty_and_keep_root(tmp_path):
    """两道闸：keep_root 永不删、非空目录不删。"""
    from skysheep.core.checkpoints import prune_created_dirs

    work = tmp_path / "work"
    deep = work / "a" / "b"
    deep.mkdir(parents=True)

    # 空目录链一路收到 keep_root 为止
    removed = prune_created_dirs(deep, keep_root=work)
    assert removed == [str(deep), str(work / "a")]
    assert work.exists()

    # 非空目录：原地停
    busy = work / "busy" / "inner"
    busy.mkdir(parents=True)
    (busy / "x.txt").write_bytes(b"x")
    assert prune_created_dirs(busy, keep_root=work) == []
    assert (busy / "x.txt").exists()

    # 没有 keep_root 时也不越过临时目录根（测试环境的 tmp_path）
    solo = tmp_path / "solo"
    solo.mkdir()
    removed = prune_created_dirs(solo, keep_root=tmp_path)
    assert removed == [str(solo)]
    assert tmp_path.exists()
