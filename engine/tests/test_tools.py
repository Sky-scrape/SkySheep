"""内置工具测试。"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.tools import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    RunCommandTool,
    ToolContext,
    ToolError,
    WriteFileTool,
)


def ctx(tmp_path):
    return ToolContext(working_dir=tmp_path)


async def test_write_then_read(tmp_path):
    w = WriteFileTool()
    out = await w.run(w.args_model(path="a.txt", content="line1\nline2"), ctx(tmp_path))
    assert "a.txt" in out
    r = ReadFileTool()
    text = await r.run(r.args_model(path="a.txt"), ctx(tmp_path))
    assert "1\tline1" in text
    assert "2\tline2" in text


async def test_read_missing_file(tmp_path):
    r = ReadFileTool()
    with pytest.raises(ToolError):
        await r.run(r.args_model(path="nope.txt"), ctx(tmp_path))


async def test_read_offset_limit(tmp_path):
    w = WriteFileTool()
    lines = "\n".join(f"l{i}" for i in range(1, 11))
    await w.run(w.args_model(path="n.txt", content=lines), ctx(tmp_path))
    r = ReadFileTool()
    text = await r.run(r.args_model(path="n.txt", offset=3, limit=2), ctx(tmp_path))
    assert "3\tl3" in text and "4\tl4" in text and "5\tl5" not in text


async def test_edit_unique_and_errors(tmp_path):
    w = WriteFileTool()
    await w.run(w.args_model(path="c.py", content="def main():\n    print('hi')\n"), ctx(tmp_path))
    e = EditFileTool()
    out = await e.run(
        e.args_model(path="c.py", old_string="print('hi')", new_string="print('bye')"),
        ctx(tmp_path),
    )
    assert "1 replacement" in out
    content = (tmp_path / "c.py").read_text(encoding="utf-8")
    assert "print('bye')" in content
    with pytest.raises(ToolError, match="not found in file"):
        await e.run(e.args_model(path="c.py", old_string="zzz", new_string="y"), ctx(tmp_path))
    with pytest.raises(ToolError, match="identical"):
        await e.run(
            e.args_model(path="c.py", old_string="bye", new_string="bye"), ctx(tmp_path)
        )


async def test_edit_ambiguous_requires_replace_all(tmp_path):
    w = WriteFileTool()
    await w.run(w.args_model(path="d.txt", content="x = 1\nx = 1\n"), ctx(tmp_path))
    e = EditFileTool()
    with pytest.raises(ToolError, match="2 locations"):
        await e.run(e.args_model(path="d.txt", old_string="x = 1", new_string="y = 1"), ctx(tmp_path))
    out = await e.run(
        e.args_model(path="d.txt", old_string="x = 1", new_string="y = 1", replace_all=True),
        ctx(tmp_path),
    )
    assert "2 replacement" in out


async def test_list_dir_and_glob(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "b.md").write_text("hello", encoding="utf-8")
    ld = ListDirTool()
    out = await ld.run(ld.args_model(), ctx(tmp_path))
    assert "sub/" in out and "b.md" in out
    g = GlobTool()
    found = await g.run(g.args_model(pattern="**/*.py"), ctx(tmp_path))
    assert "a.py" in found


async def test_grep(tmp_path):
    (tmp_path / "s.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
    g = GrepTool()
    out = await g.run(g.args_model(pattern="return 42"), ctx(tmp_path))
    assert "s.py:2" in out
    out2 = await g.run(g.args_model(pattern="nothere"), ctx(tmp_path))
    assert out2 == "(no matches)"
    with pytest.raises(ToolError, match="invalid regex"):
        await g.run(g.args_model(pattern="("), ctx(tmp_path))


async def test_run_command(tmp_path):
    tool = RunCommandTool()
    out = await tool.run(tool.args_model(command="echo sky_sheep_test"), ctx(tmp_path))
    assert "exit code: 0" in out
    assert "sky_sheep_test" in out


async def test_run_command_timeout(tmp_path):
    tool = RunCommandTool()
    if __import__("sys").platform == "win32":
        cmd = "ping -n 5 127.0.0.1"
    else:
        cmd = "sleep 5"
    with pytest.raises(ToolError, match="timed out"):
        await tool.run(tool.args_model(command=cmd, timeout_s=1), ctx(tmp_path))


async def test_run_command_timeout_keeps_partial_output(tmp_path):
    """超时报错带回超时前已产生的输出（模型能据此诊断，不再盲猜）。"""
    tool = RunCommandTool()
    if __import__("sys").platform == "win32":
        cmd = "echo partial_marker && ping -n 5 127.0.0.1 >nul"
    else:
        cmd = "echo partial_marker; sleep 5"
    with pytest.raises(ToolError, match="partial_marker"):
        await tool.run(tool.args_model(command=cmd, timeout_s=2), ctx(tmp_path))


async def test_run_command_background_lifecycle(tmp_path):
    """后台模式：启动立即返回 → read 读到输出 → kill 结束。"""
    tool = RunCommandTool()
    if __import__("sys").platform == "win32":
        cmd = "echo bg_hello_标记 & ping -n 30 127.0.0.1 >nul"
    else:
        cmd = "echo bg_hello_标记; sleep 30"
    out = await tool.run(tool.args_model(command=cmd, background=True), ctx(tmp_path))
    assert "后台进程已启动" in out
    bid = int(out.split("id=")[1].split("（")[0])

    try:
        # 轮询等输出到达（读线程异步入缓冲）
        text = ""
        for _ in range(30):
            await asyncio.sleep(0.2)
            text = await tool.run(
                tool.args_model(command="", action="read", id=bid), ctx(tmp_path))
            if "bg_hello_标记" in text:
                break
        assert "bg_hello_标记" in text, text
        assert "仍在运行" in text

        kill_out = await tool.run(
            tool.args_model(command="", action="kill", id=bid), ctx(tmp_path))
        assert "已终止" in kill_out or "早已退出" in kill_out
    finally:
        # 兜底清理（测试进程不能留孤儿）
        from skysheep.tools.shell import _BG
        st = _BG.pop(bid, None)
        if st and st["proc"].poll() is None:
            if __import__("sys").platform == "win32":
                import subprocess as _sp
                _sp.run(["taskkill.exe", "/PID", str(st["proc"].pid), "/T", "/F"],
                        capture_output=True)
            else:
                st["proc"].kill()


async def test_run_command_background_list_and_errors(tmp_path):
    tool = RunCommandTool()
    out = await tool.run(tool.args_model(command="", action="list"), ctx(tmp_path))
    assert isinstance(out, str)
    with pytest.raises(ToolError, match="不存在"):
        await tool.run(tool.args_model(command="", action="read", id=99999), ctx(tmp_path))
    with pytest.raises(ToolError, match="command 不能为空"):
        await tool.run(tool.args_model(command=""), ctx(tmp_path))


# ---- 写入端大小上限（reading 端早有截断，写入端此前无限制） ----


async def test_write_file_rejects_oversized_content(tmp_path, monkeypatch):
    """超过上限的写入直接报错（不截断）：截断会写出半个文件却报告成功。"""
    from skysheep.tools import base as base_mod

    monkeypatch.setattr(base_mod, "MAX_WRITE_CHARS", 100)
    w = WriteFileTool()
    with pytest.raises(ToolError, match="写入内容过大"):
        await w.run(w.args_model(path="big.txt", content="x" * 101), ctx(tmp_path))
    assert not (tmp_path / "big.txt").exists(), "被拒绝的写入不应留下任何文件"
    # 刚好到上限仍可写
    await w.run(w.args_model(path="ok.txt", content="x" * 100), ctx(tmp_path))
    assert (tmp_path / "ok.txt").stat().st_size == 100


async def test_edit_file_allows_shrinking_oversized_file(tmp_path, monkeypatch):
    """存量文件已经超上限时，仍然允许把它改小（不因存量挡住修复动作）。"""
    from skysheep.tools import base as base_mod

    monkeypatch.setattr(base_mod, "MAX_WRITE_CHARS", 100)
    target = tmp_path / "legacy.txt"
    target.write_text("y" * 500, encoding="utf-8")
    e = EditFileTool()
    out = await e.run(
        e.args_model(path="legacy.txt", old_string="y" * 500, new_string="y" * 50),
        ctx(tmp_path),
    )
    assert "1 replacement" in out
    assert target.stat().st_size == 50


async def test_edit_file_rejects_growing_beyond_limit(tmp_path, monkeypatch):
    """把文件改到超过上限则拒绝（改大同样受约束）。"""
    from skysheep.tools import base as base_mod

    monkeypatch.setattr(base_mod, "MAX_WRITE_CHARS", 100)
    w = WriteFileTool()
    await w.run(w.args_model(path="g.txt", content="a" * 50), ctx(tmp_path))
    e = EditFileTool()
    with pytest.raises(ToolError, match="写入内容过大"):
        await e.run(
            e.args_model(path="g.txt", old_string="a" * 50, new_string="a" * 200),
            ctx(tmp_path),
        )


async def test_write_document_rejects_oversized_content(tmp_path, monkeypatch):
    """write_document 与 write_file 同一套上限口径。"""
    from skysheep.tools import base as base_mod
    from skysheep.tools.docs import WriteDocumentTool

    monkeypatch.setattr(base_mod, "MAX_WRITE_CHARS", 100)
    d = WriteDocumentTool()
    with pytest.raises(ToolError, match="写入内容过大"):
        await d.run(d.args_model(path="r.csv", content="x" * 101), ctx(tmp_path))
