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


# ---------------------------------------------------------------- 权限门与新增工具


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
