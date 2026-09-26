"""文本编码 / 行尾符保持、图片读取、文件增删移工具。

对应修复：中文 Windows 上 GBK 文本文件被 read_file 读成替换字符、一旦写回
原文永久损坏；写回时 LF 被静默换成 CRLF；read_file 对二进制不做拦截；
移动/删除文件只能走 run_command（绕过权限门且不进检查点）。
"""

from __future__ import annotations

import base64

import pytest

from skysheep.core.checkpoints import CheckpointStore
from skysheep.textio import (
    decode_bytes,
    detect_newline,
    encode_text,
    from_lf,
    read_text_file,
    sniff,
    to_lf,
)
from skysheep.tools import ChangeRecorder, ToolContext, ToolError, ToolRegistry, default_tools
from skysheep.tools.fs import (
    DeleteFileArgs,
    EditFileArgs,
    MakeDirArgs,
    MoveFileArgs,
    ReadFileArgs,
    WriteFileArgs,
)
from skysheep.tools.image import ReadImageArgs

# ---------------------------------------------------------------- textio 单元


def test_detect_newline():
    assert detect_newline("a\nb\n") == "\n"
    assert detect_newline("a\r\nb\r\n") == "\r\n"
    assert detect_newline("a\rb\r") == "\r"
    assert detect_newline("no newline") == "\n"
    # 混合时取多数：两处 CRLF 对一处 LF
    assert detect_newline("a\r\nb\r\nc\n") == "\r\n"


def test_to_lf_and_from_lf_roundtrip():
    assert to_lf("a\r\nb\rc\n") == "a\nb\nc\n"
    assert from_lf("a\nb\n", "\r\n") == "a\r\nb\r\n"
    assert from_lf("a\nb\n", "\n") == "a\nb\n"


def test_sniff_utf8_ascii():
    enc, certain, binary = sniff(b"hello")
    assert (enc, certain, binary) == ("utf-8", True, False)


def test_sniff_gb18030():
    data = "中文测试内容".encode("gb18030")
    enc, certain, binary = sniff(data)
    assert enc == "gb18030" and certain and not binary
    assert data.decode(enc) == "中文测试内容"


def test_sniff_bom():
    for enc in ("utf-8-sig", "utf-16", "utf-32"):
        data = "中文内容".encode(enc)
        got, certain, binary = sniff(data)
        assert got == enc and certain and not binary, enc


def test_sniff_binary_detected():
    enc, certain, binary = sniff(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    assert binary and enc is None


def test_sniff_unknown_encoding_not_certain():
    # 既不是合法 UTF-8 也不是合法 GB18030（0x80 单独出现不构成 GB18030 双字节）
    enc, certain, binary = sniff(b"\x80\x80\x80text")
    assert enc is None and not certain and not binary


def test_decode_gbk_file_keeps_text(tmp_path):
    p = tmp_path / "gbk.txt"
    p.write_bytes("第一行内容\n第二行内容\n".encode("gbk"))
    loaded = read_text_file(p)
    assert loaded.text == "第一行内容\n第二行内容\n"
    assert loaded.encoding == "gb18030" and loaded.certain


def test_decode_crlf_reported_and_normalized(tmp_path):
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"a\r\nb\r\n")
    loaded = read_text_file(p)
    assert loaded.newline == "\r\n"
    assert loaded.text == "a\nb\n"  # 内部统一 LF


def test_encode_text_restores_newline():
    assert encode_text("a\nb\n", "utf-8", "\r\n") == b"a\r\nb\r\n"
    assert encode_text("a\nb\n", "gb18030", "\n") == "a\nb\n".encode("gb18030")


def test_decode_bytes_binary_flag():
    loaded = decode_bytes(b"\x00\x01\x02")
    assert loaded.binary and loaded.text == ""


# ---------------------------------------------------------------- 工具层：编码


def _ctx(tmp_path):
    """工具层测试的公共上下文：绑定 tmp_path 作为工作目录。"""
    return ToolContext(tmp_path)


async def test_edit_gbk_file_preserves_encoding(tmp_path):
    """GBK 文件编辑后仍是 GBK，中文不丢——此前会被 U+FFFD 永久覆盖。"""
    p = tmp_path / "note.txt"
    orig = "第一行内容\n第二行内容\n"
    p.write_bytes(orig.encode("gbk"))
    tool = ToolRegistry(default_tools()).get("edit_file")
    out = await tool.run(
        EditFileArgs(path="note.txt", old_string="第二行内容", new_string="第二行已改"),
        _ctx(tmp_path),
    )
    assert "1 replacement" in out
    raw = p.read_bytes()
    assert raw.decode("gbk") == "第一行内容\n第二行已改\n"
    assert b"\xef\xbf\xbd" not in raw  # 没有替换字符


async def test_edit_crlf_file_keeps_crlf(tmp_path):
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"alpha\r\nbeta\r\n")
    tool = ToolRegistry(default_tools()).get("edit_file")
    await tool.run(
        EditFileArgs(path="crlf.txt", old_string="beta", new_string="gamma"),
        _ctx(tmp_path),
    )
    assert p.read_bytes() == b"alpha\r\ngamma\r\n"


async def test_edit_lf_file_keeps_lf(tmp_path):
    p = tmp_path / "lf.txt"
    p.write_bytes(b"alpha\nbeta\n")
    tool = ToolRegistry(default_tools()).get("edit_file")
    await tool.run(
        EditFileArgs(path="lf.txt", old_string="beta", new_string="gamma"),
        _ctx(tmp_path),
    )
    assert p.read_bytes() == b"alpha\ngamma\n"


async def test_write_existing_gbk_file_keeps_encoding(tmp_path):
    p = tmp_path / "g.txt"
    p.write_bytes("旧内容\n".encode("gbk"))
    tool = ToolRegistry(default_tools()).get("write_file")
    await tool.run(WriteFileArgs(path="g.txt", content="新内容\n"), _ctx(tmp_path))
    assert p.read_bytes().decode("gbk") == "新内容\n"


async def test_write_new_file_is_utf8_lf(tmp_path):
    tool = ToolRegistry(default_tools()).get("write_file")
    await tool.run(WriteFileArgs(path="new.txt", content="hello\n"), _ctx(tmp_path))
    assert (tmp_path / "new.txt").read_bytes() == b"hello\n"


async def test_read_file_reports_encoding_note(tmp_path):
    (tmp_path / "gbk.txt").write_bytes("中文".encode("gbk"))
    tool = ToolRegistry(default_tools()).get("read_file")
    out = await tool.run(ReadFileArgs(path="gbk.txt"), _ctx(tmp_path))
    assert "中文" in out
    assert "gb18030" in out


async def test_edit_refuses_unknown_encoding(tmp_path):
    """编码探不出来时拒绝编辑，而不是用替换字符覆盖原文。"""
    p = tmp_path / "weird.bin"
    p.write_bytes(b"\x80\x80\x80text")
    tool = ToolRegistry(default_tools()).get("edit_file")
    before = p.read_bytes()
    with pytest.raises(ToolError) as ei:
        await tool.run(
            EditFileArgs(path="weird.bin", old_string="text", new_string="changed"),
            _ctx(tmp_path),
        )
    assert "编码无法确定" in str(ei.value)
    assert p.read_bytes() == before  # 磁盘未被触碰


# ---------------------------------------------------------------- 工具层：二进制拦截


async def test_read_file_rejects_binary(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00\x01\x02binary")
    tool = ToolRegistry(default_tools()).get("read_file")
    with pytest.raises(ToolError) as ei:
        await tool.run(ReadFileArgs(path="data.bin"), _ctx(tmp_path))
    assert "二进制" in str(ei.value)


async def test_read_file_hints_read_image_for_png(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")
    tool = ToolRegistry(default_tools()).get("read_file")
    with pytest.raises(ToolError) as ei:
        await tool.run(ReadFileArgs(path="pic.png"), _ctx(tmp_path))
    assert "read_image" in str(ei.value)


# ---------------------------------------------------------------- read_image


def _png_bytes(w: int = 8, h: int = 8) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


async def test_read_image_attaches_to_context(tmp_path):
    (tmp_path / "pic.png").write_bytes(_png_bytes())
    tool = ToolRegistry(default_tools()).get("read_image")
    ctx = _ctx(tmp_path)
    out = await tool.run(ReadImageArgs(path="pic.png"), ctx)
    assert "pic.png" in out
    assert len(ctx.images) == 1
    assert ctx.images[0].media_type == "image/png"
    assert base64.b64decode(ctx.images[0].data).startswith(b"\x89PNG")


async def test_read_image_shrinks_wide_image(tmp_path):
    (tmp_path / "wide.png").write_bytes(_png_bytes(3200, 100))
    tool = ToolRegistry(default_tools()).get("read_image")
    ctx = _ctx(tmp_path)
    out = await tool.run(ReadImageArgs(path="wide.png"), ctx)
    assert "缩小" in out
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(ctx.images[0].data)))
    assert img.width == 1568


async def test_read_image_rejects_non_image_extension(tmp_path):
    (tmp_path / "doc.txt").write_text("hi", encoding="utf-8")
    tool = ToolRegistry(default_tools()).get("read_image")
    with pytest.raises(ToolError) as ei:
        await tool.run(ReadImageArgs(path="doc.txt"), _ctx(tmp_path))
    assert "不是支持的图片格式" in str(ei.value)


async def test_read_image_rejects_fake_png(tmp_path):
    (tmp_path / "fake.png").write_bytes(b"not really a png at all")
    tool = ToolRegistry(default_tools()).get("read_image")
    with pytest.raises(ToolError) as ei:
        await tool.run(ReadImageArgs(path="fake.png"), _ctx(tmp_path))
    assert "不是有效图片" in str(ei.value)


async def test_read_image_needs_vision(tmp_path):
    (tmp_path / "pic.png").write_bytes(_png_bytes())
    tool = ToolRegistry(default_tools()).get("read_image")
    ctx = ToolContext(tmp_path, supports_vision=False)
    with pytest.raises(ToolError) as ei:
        await tool.run(ReadImageArgs(path="pic.png"), ctx)
    assert "不支持图片输入" in str(ei.value)


# ---------------------------------------------------------------- move / delete / mkdir


async def test_move_file_renames(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    tool = ToolRegistry(default_tools()).get("move_file")
    out = await tool.run(
        MoveFileArgs(source="a.txt", destination="b.txt"), _ctx(tmp_path)
    )
    assert "moved" in out
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"


async def test_move_file_into_dir_keeps_name(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    tool = ToolRegistry(default_tools()).get("move_file")
    await tool.run(MoveFileArgs(source="a.txt", destination="docs"), _ctx(tmp_path))
    assert (tmp_path / "docs" / "a.txt").exists()


async def test_move_file_refuses_existing_target_without_overwrite(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    tool = ToolRegistry(default_tools()).get("move_file")
    with pytest.raises(ToolError) as ei:
        await tool.run(MoveFileArgs(source="a.txt", destination="b.txt"), _ctx(tmp_path))
    assert "目标已存在" in str(ei.value)
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "b"


async def test_move_file_overwrite_when_asked(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    tool = ToolRegistry(default_tools()).get("move_file")
    await tool.run(
        MoveFileArgs(source="a.txt", destination="b.txt", overwrite=True), _ctx(tmp_path)
    )
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "a"


async def test_move_file_refuses_self_nesting(tmp_path):
    (tmp_path / "d").mkdir()
    tool = ToolRegistry(default_tools()).get("move_file")
    with pytest.raises(ToolError) as ei:
        await tool.run(MoveFileArgs(source="d", destination="d/sub"), _ctx(tmp_path))
    assert "自包含递归" in str(ei.value)


async def test_make_dir_is_idempotent(tmp_path):
    tool = ToolRegistry(default_tools()).get("make_dir")
    out1 = await tool.run(MakeDirArgs(path="a/b/c"), _ctx(tmp_path))
    assert "created directory" in out1
    assert (tmp_path / "a" / "b" / "c").is_dir()
    out2 = await tool.run(MakeDirArgs(path="a/b/c"), _ctx(tmp_path))
    assert "已存在" in out2


async def test_delete_file_removes_file(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    tool = ToolRegistry(default_tools()).get("delete_file")
    out = await tool.run(DeleteFileArgs(path="a.txt"), _ctx(tmp_path))
    assert "deleted" in out
    assert not (tmp_path / "a.txt").exists()


async def test_delete_file_requires_recursive_for_nonempty_dir(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.txt").write_text("x", encoding="utf-8")
    tool = ToolRegistry(default_tools()).get("delete_file")
    with pytest.raises(ToolError) as ei:
        await tool.run(DeleteFileArgs(path="d"), _ctx(tmp_path))
    assert "recursive=true" in str(ei.value)
    assert (tmp_path / "d" / "x.txt").exists()
    await tool.run(DeleteFileArgs(path="d", recursive=True), _ctx(tmp_path))
    assert not (tmp_path / "d").exists()


async def test_delete_directory_is_recoverable_by_checkpoint(tmp_path):
    """删目录进检查点，撤销能整体还原（此前只能 run_command，无法回滚）。"""
    rec = ChangeRecorder()
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.txt").write_text("aaa", encoding="utf-8")
    (tmp_path / "d" / "b.txt").write_text("bbb", encoding="utf-8")
    tool = ToolRegistry(default_tools(recorder=rec)).get("delete_file")
    await tool.run(DeleteFileArgs(path="d", recursive=True), _ctx(tmp_path))
    assert not (tmp_path / "d").exists()

    store = CheckpointStore()
    saved = store.save("s1", dict(rec.pre))
    assert saved is not None
    store.restore(saved["id"])
    assert (tmp_path / "d" / "a.txt").read_text(encoding="utf-8") == "aaa"
    assert (tmp_path / "d" / "b.txt").read_text(encoding="utf-8") == "bbb"


async def test_move_is_recoverable_by_checkpoint(tmp_path):
    """移动进检查点：撤销后源文件回来、目标位置清空。"""
    rec = ChangeRecorder()
    (tmp_path / "a.txt").write_text("payload", encoding="utf-8")
    tool = ToolRegistry(default_tools(recorder=rec)).get("move_file")
    await tool.run(MoveFileArgs(source="a.txt", destination="docs/b.txt"), _ctx(tmp_path))
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "docs" / "b.txt").exists()

    store = CheckpointStore()
    saved = store.save("s1", dict(rec.pre))
    store.restore(saved["id"])
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "payload"
    assert not (tmp_path / "docs" / "b.txt").exists()


async def test_move_dir_is_fully_recoverable_by_checkpoint(tmp_path):
    """目录移动逐文件进检查点：撤销后源树整体还原、目标处清空。

    旧实现把源目录记成 None（语义=改前不存在），回滚删得掉移过去的树
    却还原不出源——目录内容直接丢失（审查 A-3 附带缺陷）。
    """
    rec = ChangeRecorder()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("aaa", encoding="utf-8")
    (tmp_path / "src" / "sub").mkdir()
    (tmp_path / "src" / "sub" / "b.txt").write_text("bbb", encoding="utf-8")
    tool = ToolRegistry(default_tools(recorder=rec)).get("move_file")
    await tool.run(MoveFileArgs(source="src", destination="dst"), _ctx(tmp_path))
    assert (tmp_path / "dst" / "sub" / "b.txt").exists()

    store = CheckpointStore()
    saved = store.save("s1", dict(rec.pre))
    store.restore(saved["id"])
    assert (tmp_path / "src" / "a.txt").read_text(encoding="utf-8") == "aaa"
    assert (tmp_path / "src" / "sub" / "b.txt").read_text(encoding="utf-8") == "bbb"
    assert not (tmp_path / "dst").exists()


async def test_move_overwrite_dir_checkpoint_restores_both_sides(tmp_path):
    """覆盖已存在目录的移动：回滚后源树、被覆盖的旧内容都在，移入副本清干净。

    覆盖 = 对目标子树 rmtree（审查 A-3 主缺陷）：旧实现目标旧内容不进
    检查点、目标目录被记成 None（回滚时二次删除），整树数据不可恢复。
    """
    rec = ChangeRecorder()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "new.txt").write_text("new", encoding="utf-8")
    (tmp_path / "dst" / "src").mkdir(parents=True)
    (tmp_path / "dst" / "src" / "old.txt").write_text("old", encoding="utf-8")
    tool = ToolRegistry(default_tools(recorder=rec)).get("move_file")
    await tool.run(
        MoveFileArgs(source="src", destination="dst", overwrite=True), _ctx(tmp_path))
    assert (tmp_path / "dst" / "src" / "new.txt").exists()
    assert not (tmp_path / "dst" / "src" / "old.txt").exists()

    store = CheckpointStore()
    saved = store.save("s1", dict(rec.pre))
    store.restore(saved["id"])
    assert (tmp_path / "src" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "dst" / "src" / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "dst" / "src" / "new.txt").exists(), "移入副本应被回滚清掉"


# ---------------------------------------------------------------- 权限门与新增工具


def test_checkpoint_prune_is_per_session(tmp_path):
    """淘汰按会话分桶：一个会话轮次再多也不挤掉另一个会话的快照。

    并行会话（或流水线节点）各写各的目录时，旧实现按全局 FIFO 淘汰，
    活跃会话会把别的会话还能回滚的快照挤掉。
    """
    from skysheep.core.checkpoints import MAX_CHECKPOINTS

    store = CheckpointStore()  # 纯内存（不落盘）
    other = tmp_path / "other.txt"
    other.write_text("x", encoding="utf-8")
    keep = store.save("B", {str(other): b"before-b"})
    same = tmp_path / "same.txt"
    same.write_text("y", encoding="utf-8")
    for _ in range(MAX_CHECKPOINTS + 3):
        store.save("A", {str(same): b"before-a"})

    assert keep["id"] in {c["id"] for c in store.list_for("B")}, "别的会话的配额不该挤掉 B"
    assert len(store.list_for("A")) == MAX_CHECKPOINTS


def test_checkpoint_restore_conflicts_when_file_changed_after_save(tmp_path):
    """快照之后文件又被改过：回滚前抛冲突（防抹掉并行改动），force 才覆盖。"""
    from skysheep.core.checkpoints import CheckpointConflictError

    store = CheckpointStore()
    p = tmp_path / "doc.txt"
    p.write_text("旧内容", encoding="utf-8")
    cp = store.save("s1", {str(p): b"before"})
    # 保存之后文件被改了（模拟并行任务/用户手改）
    p.write_text("别人改的", encoding="utf-8")
    with pytest.raises(CheckpointConflictError) as ei:
        store.restore(cp["id"])
    assert str(p) in ei.value.conflicts
    assert p.read_text(encoding="utf-8") == "别人改的", "冲突时不得覆盖"

    store.restore(cp["id"], force=True)  # 用户确认后强制回滚
    assert p.read_text(encoding="utf-8") == "before"


def test_checkpoint_legacy_meta_without_sigs_skips_dirty_check(tmp_path):
    """旧版快照（meta 里没有 sigs 字段）不做脏检查，回滚保持旧行为。"""
    import json as _json

    root = tmp_path / "cps"
    store = CheckpointStore(root)
    p = tmp_path / "legacy.txt"
    p.write_text("旧", encoding="utf-8")
    cp = store.save("s1", {str(p): b"before"})

    # 手工抹掉 meta 里的 sigs，模拟旧版（升级前）写下的快照
    meta_path = store._cp_dir(store._items[cp["id"]]) / "meta.json"
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("sigs", None)
    meta_path.write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    reloaded = CheckpointStore(root)  # 重新加载：sigs 是空表

    p.write_text("又被改了", encoding="utf-8")
    reloaded.restore(cp["id"])  # 不做脏检查，不抛冲突
    assert p.read_text(encoding="utf-8") == "before"


def test_move_write_target_is_destination():
    """自动允许写入档按 destination 判目录边界，而不是 source。"""
    from pathlib import Path

    from skysheep.security.gate import PermissionGate

    gate = PermissionGate(working_dir=Path.cwd())
    tool = ToolRegistry(default_tools()).get("move_file")
    inside = {"source": "a.txt", "destination": "sub/b.txt"}
    assert gate._write_target_inside_workdir(tool, inside)
    # 目标是绝对路径逃出工作目录 → 不放行
    assert not gate._write_target_inside_workdir(
        tool, {"source": "a.txt", "destination": "C:/Windows/evil.txt"}
    )
    # 源在工作目录外（把外部文件移进来）→ 也不放行，回退逐次确认
    assert not gate._write_target_inside_workdir(
        tool, {"source": "C:/Windows/evil.txt", "destination": "b.txt"}
    )


def test_delete_file_is_dangerous_safety():
    tool = ToolRegistry(default_tools()).get("delete_file")
    from skysheep.tools.base import Safety

    assert tool.safety == Safety.DANGEROUS


def test_move_and_mkdir_are_write_safety():
    from skysheep.tools.base import Safety

    reg = ToolRegistry(default_tools())
    assert reg.get("move_file").safety == Safety.WRITE
    assert reg.get("make_dir").safety == Safety.WRITE


def test_new_tools_registered():
    names = {t.name for t in default_tools()}
    assert {"read_image", "move_file", "delete_file", "make_dir"} <= names


def test_move_rule_is_prefix_free_always():
    """move_file 没有动作词，固化规则退化为整工具放行属预期（用户显式选择）。"""
    from skysheep.security.gate import PermissionGate

    tool = ToolRegistry(default_tools()).get("move_file")
    rule = PermissionGate.rule_for(tool, {"source": "a", "destination": "b"})
    assert rule.tool == "move_file"


async def test_last_diff_lives_on_context_not_tool_instance(tmp_path):
    """diff 记在 ctx 上、不落在工具实例上：实例被并行任务共享，
    实例属性会把另一个任务的 diff 错配给当前调用。"""
    import asyncio

    tool = ToolRegistry(default_tools()).get("write_file")
    ctx_a = ToolContext(tmp_path, session_id="A")
    ctx_b = ToolContext(tmp_path, session_id="B")

    await asyncio.gather(
        tool.run(WriteFileArgs(path="a.txt", content="AAA"), ctx_a),
        tool.run(WriteFileArgs(path="b.txt", content="BBB"), ctx_b),
    )
    assert "a.txt" in ctx_a.last_diff and "b.txt" not in ctx_a.last_diff
    assert "b.txt" in ctx_b.last_diff and "a.txt" not in ctx_b.last_diff


def test_write_text_file_is_atomic_no_residue(tmp_path):
    """原子写：落盘后不留临时文件（同目录 .tmp 中间态不外泄）。"""
    from skysheep.textio import write_text_file

    p = tmp_path / "note.txt"
    write_text_file(p, "第一行\n第二行\n", "utf-8", "\n")
    assert p.read_text(encoding="utf-8") == "第一行\n第二行\n"
    assert [f.name for f in tmp_path.iterdir()] == ["note.txt"], "临时文件必须清理"


def test_write_text_file_replace_failure_keeps_original(tmp_path, monkeypatch):
    """替换失败（进程被杀/目标被占用）时旧内容原样保留，临时文件清理掉。"""
    import os as _os

    from skysheep.textio import write_text_file

    p = tmp_path / "note.txt"
    write_text_file(p, "原始内容\n", "utf-8", "\n")

    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        write_text_file(p, "新内容\n", "utf-8", "\n")
    monkeypatch.undo()
    assert p.read_text(encoding="utf-8") == "原始内容\n", "失败时旧内容必须原样保留"
    assert [f.name for f in tmp_path.iterdir()] == ["note.txt"]



def test_write_text_atomic_replaces_and_leaves_no_temp(tmp_path):
    """引擎自有状态文件的原子写：覆盖后目标完整，临时文件不残留。"""
    from skysheep.textio import write_text_atomic

    target = tmp_path / "sub" / "config.toml"
    write_text_atomic(target, "a = 1\n")
    assert target.read_text(encoding="utf-8") == "a = 1\n"
    write_text_atomic(target, "a = 2\n")  # 覆盖写
    assert target.read_text(encoding="utf-8") == "a = 2\n"
    leftovers = [f.name for f in target.parent.iterdir() if f.name != "config.toml"]
    assert leftovers == [], leftovers


def test_write_text_atomic_keeps_old_content_on_failure(tmp_path):
    """写失败（编码错误）时旧内容原样保留，不留半个文件。"""
    from skysheep.textio import write_text_atomic

    target = tmp_path / "state.json"
    payload = '{"ok": true}'
    write_text_atomic(target, payload)
    with pytest.raises(UnicodeEncodeError):
        write_text_atomic(target, "坏的" + "\ud800", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == payload
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "state.json"]
    assert leftovers == [], leftovers


def test_config_writes_are_atomic_and_clamped(home):
    """config.toml：越界/非数字值夹回合法区间；写入后文件完整可解析。"""
    import tomllib

    from skysheep.config import config_path, load_config, update_config_section

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "max_iterations = 9999\n"
        "compaction_keep_recent = 0\n"
        "subagent_max_concurrent = 100\n"
        'context_limit_tokens = "not-a-number"\n'
        "daily_token_budget = -5\n",
        encoding="utf-8",
    )
    cfg = load_config()  # 不能抛 ValidationError
    assert cfg.max_iterations == 200          # le=200 上界
    assert cfg.compaction_keep_recent == 2    # ge=2 下界
    assert cfg.subagent_max_concurrent == 8   # le=8 上界
    assert cfg.context_limit_tokens == 1_000_000  # 非数字回默认
    assert cfg.daily_token_budget == 0        # 负值夹到 0

    update_config_section("memory", {"digest_enabled": True})
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)  # 写回后仍是完整 TOML
    leftovers = [f.name for f in p.parent.iterdir() if f.name != p.name]
    assert leftovers == [], leftovers


# ---- M15：检查点字节上限（条数限制挡不住大文件） ----


def test_checkpoint_skips_oversized_file(tmp_path, monkeypatch):
    """单个超大文件不进快照（其余文件照常可回滚），不因它丢掉整条检查点。"""
    import skysheep.core.checkpoints as ckpt

    monkeypatch.setattr(ckpt, "MAX_CHECKPOINT_FILE_BYTES", 1024)
    store = ckpt.CheckpointStore()
    small = tmp_path / "small.txt"
    small.write_text("小", encoding="utf-8")
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2048)
    cp = store.save("s1", {
        str(small): b"old-small",
        str(big): b"y" * 2048,
    })
    assert cp["paths"] == [str(small)], "超限的大文件应被跳过"
    assert cp["skipped"] == [str(big)]
    store.restore(cp["id"])
    assert small.read_text(encoding="utf-8") == "old-small"

    # 全都超限时整条不存（返回 None），不产生空检查点
    assert store.save("s1", {str(big): b"z" * 2048}) is None


def test_checkpoint_total_bytes_evicts_oldest(tmp_path, monkeypatch):
    """全库字节上限：条数没超但总量超了，按时间淘汰最旧的。"""
    import skysheep.core.checkpoints as ckpt

    monkeypatch.setattr(ckpt, "MAX_CHECKPOINT_BYTES", 100)
    monkeypatch.setattr(ckpt, "MAX_CHECKPOINT_TOTAL_BYTES", 250)
    store = ckpt.CheckpointStore()
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    ids = []
    for _ in range(5):  # 每条 100 字节：第 4 条起开始淘汰最旧的
        cp = store.save("s1", {str(p): b"z" * 100})
        ids.append(cp["id"])
    assert ids[0] not in store._items, "超出全库字节上限时应淘汰最旧的"
    assert ids[-1] in store._items
    total = sum(int(c.get("bytes") or 0) for c in store._items.values())
    assert total <= 250

# ---- 低危项：grep 支持非 UTF-8 文本 ----


def test_grep_matches_gbk_file(tmp_path):
    """GBK/GB18030 中文文件能被搜到（旧实现固定按 UTF-8 读，整片漏检）。"""
    import asyncio

    from skysheep.tools.search import GrepArgs, GrepTool

    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes("中文内容：密钥在这里\n第二行".encode("gbk"))
    utf8 = tmp_path / "utf8.txt"
    utf8.write_text("中文内容：另一份\n", encoding="utf-8")
    ctx = ToolContext(working_dir=tmp_path)

    out = asyncio.run(GrepTool().run(GrepArgs(pattern="密钥"), ctx))
    assert "gbk.txt:1" in out, out
    assert "utf8.txt" not in out

    # 两份文件都要能被同一个模式命中（编码不影响匹配）
    out2 = asyncio.run(GrepTool().run(GrepArgs(pattern="中文内容"), ctx))
    assert "gbk.txt:1" in out2 and "utf8.txt:1" in out2

    # 二进制不参与匹配（也不因解码失败误报）
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x00")
    out3 = asyncio.run(GrepTool().run(GrepArgs(pattern="密钥"), ctx))
    assert "blob.dat" not in out3
