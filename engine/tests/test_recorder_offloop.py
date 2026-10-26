"""审查 SEC-M10：检查点记录的磁盘读取离开事件循环。

record / record_tree 此前在事件循环上同步整读改前内容：大文件、大目录会把
全部会话的流式 UI 冻住数秒到数十秒，且读取阶段内存没有快照上限兜底
（32MB/256MB 上限要到轮末 save 的 _limit_pre 才生效）。现在读取进
asyncio.to_thread；record_tree 逐文件读取前先 stat，超过
MAX_CHECKPOINT_FILE_BYTES 的直接跳过（save 端 _limit_pre 反正会丢弃，
白读还撑内存）。

M09 语义保持不变：读失败拒绝操作、目录记 DIR_MARKER、None 仅限确实不存在、
同一轮去重仍取第一次（改前）状态。既有 M09 用例在 test_textio_and_files.py。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from skysheep.core import checkpoints
from skysheep.tools.base import DIR_MARKER, ChangeRecorder, ToolError


def _spy_read_bytes(seen: list[int]):
    """包一层 read_bytes 记录调用线程 id：断言读取发生在工作线程（确定性）。"""
    real_read_bytes = Path.read_bytes

    def spy(self):
        seen.append(threading.get_ident())
        return real_read_bytes(self)

    return spy


def _deny_read_bytes(target: Path):
    """把 Path.read_bytes 打桩成对 target 抛 PermissionError（模拟被占用）。"""
    real_read_bytes = Path.read_bytes

    def denied(self):
        if self == target:
            raise PermissionError(13, "模拟文件被占用")
        return real_read_bytes(self)

    return denied


async def test_record_reads_off_event_loop(tmp_path, monkeypatch):
    """record 的整读在工作线程执行，不在事件循环线程上。"""
    seen: list[int] = []
    monkeypatch.setattr(Path, "read_bytes", _spy_read_bytes(seen))
    p = tmp_path / "a.txt"
    p.write_bytes(b"payload")

    rec = ChangeRecorder()
    await rec.record(p)

    assert seen, "record 必须真的读了盘（打桩未被命中）"
    assert seen[0] != threading.get_ident(), "读取不得发生在事件循环线程"
    assert rec.pre[str(p)] == b"payload"


async def test_record_tree_reads_off_event_loop(tmp_path, monkeypatch):
    """record_tree 的逐文件读取同样全部在工作线程执行。"""
    seen: list[int] = []
    monkeypatch.setattr(Path, "read_bytes", _spy_read_bytes(seen))
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_bytes(b"a")
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_bytes(b"b")

    rec = ChangeRecorder()
    await rec.record_tree(d)

    assert seen, "record_tree 必须真的读了盘（打桩未被命中）"
    assert all(tid != threading.get_ident() for tid in seen), "读取不得发生在事件循环线程"
    assert rec.pre[str(d / "a.txt")] == b"a"
    assert rec.pre[str(d / "sub" / "b.txt")] == b"b"


async def test_record_tool_error_propagates_from_thread(tmp_path, monkeypatch):
    """to_thread 里抛出的 ToolError 原样传回调用方，且不留半条记录（M09）。"""
    p = tmp_path / "locked.txt"
    p.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", _deny_read_bytes(p))

    rec = ChangeRecorder()
    with pytest.raises(ToolError):
        await rec.record(p)
    assert str(p) not in rec.pre, "读失败不得记 None、也不得留半条记录"
    assert p.read_text(encoding="utf-8") == "keep"


async def test_record_tree_scan_failure_propagates_from_thread(tmp_path, monkeypatch):
    """扫描失败（os.walk onerror）在 worker 线程抛 ToolError，照常传回且
    不留半棵树（M09）。"""
    real_scandir = os.scandir

    def denied_scandir(path):
        if str(path) == str(tmp_path / "d" / "guard"):
            raise PermissionError(13, "模拟拒绝访问")
        return real_scandir(path)

    d = tmp_path / "d"
    (d / "guard").mkdir(parents=True)
    (d / "a.txt").write_bytes(b"a")
    (d / "guard" / "secret.txt").write_bytes(b"s")
    monkeypatch.setattr(os, "scandir", denied_scandir)

    rec = ChangeRecorder()
    with pytest.raises(ToolError):
        await rec.record_tree(d)
    assert not rec.pre, "扫描失败不得留下半棵树的记录"


async def test_record_tree_skips_oversized_files(tmp_path, monkeypatch):
    """超过 MAX_CHECKPOINT_FILE_BYTES 的文件不读、不进 pre；其余文件照记。

    save 端 _limit_pre 反正会把超限条目丢弃（不进快照），这里提前跳过省掉
    整份读盘与内存占用。monkeypatch 上限到一个小值，避免测试真写 32MB。
    """
    monkeypatch.setattr(checkpoints, "MAX_CHECKPOINT_FILE_BYTES", 8)
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "big.bin").write_bytes(b"B" * 64)
    (d / "small.txt").write_bytes(b"abc")
    (d / "sub" / "ok.txt").write_bytes(b"hello!")

    rec = ChangeRecorder()
    await rec.record_tree(d)

    assert str(d / "big.bin") not in rec.pre, "超限文件不得进 pre"
    assert rec.pre[str(d / "small.txt")] == b"abc"
    assert rec.pre[str(d / "sub" / "ok.txt")] == b"hello!"


async def test_record_dedup_keeps_first_state(tmp_path):
    """同一轮去重语义不变：第一次记下的（改前）状态不被后续覆盖。"""
    p = tmp_path / "a.txt"
    p.write_bytes(b"first")
    rec = ChangeRecorder()
    await rec.record(p)
    p.write_bytes(b"second")  # 同轮内的变化不得污染已记的改前状态
    await rec.record(p)
    assert rec.pre[str(p)] == b"first"


async def test_record_directory_through_thread_marks_dir_sentinel(tmp_path):
    """M09 语义经异步路径不变：目录读失败（read_bytes 对目录抛 OSError）走
    is_dir → DIR_MARKER 分支，回滚时重建目录本身。"""
    d = tmp_path / "d"
    d.mkdir()
    (d / "f.txt").write_bytes(b"x")
    rec = ChangeRecorder()
    await rec.record(d)
    assert rec.pre[str(d)] == DIR_MARKER
