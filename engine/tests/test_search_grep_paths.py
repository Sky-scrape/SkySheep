"""grep 工具的代码路径覆盖：ripgrep 命令构造 / 输出后处理 / 内置扫描边界。

test_search_ripgrep.py 比的是「rg 与内置两路结果一致」，但它整组依赖本机
装没装 rg（无 rg 就跳过）。本文件用假 rg（monkeypatch shutil.which 与
subprocess.run）把命令构造与 _format_rg_output 后处理都覆盖到，并用内置
路径覆盖二进制跳过、include 过滤、超大文件、读不了、上限截断等边界分支，
保证换台机器/CI 无 rg 时这条链路也有回归。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from skysheep.tools.base import ToolContext, ToolError
from skysheep.tools.search import (
    MAX_MATCHES,
    MAX_PER_FILE,
    GrepArgs,
    GrepTool,
)


def _ctx(tmp_path, use_ripgrep: bool = True, working_dir=None) -> ToolContext:
    return ToolContext(working_dir=working_dir or tmp_path, use_ripgrep=use_ripgrep)


# ---- _format_rg_output：rg 输出后处理（纯函数） ----


def test_format_rg_output_strips_dot_slash_and_noise(tmp_path):
    stdout = "./app.py:3:hello\n./sub/inner.py:7:world\n\nnot-a-match-line\n"
    out = GrepTool._format_rg_output(stdout, tmp_path, _ctx(tmp_path))
    assert out.splitlines() == ["app.py:3:hello", "sub/inner.py:7:world"]


def test_format_rg_output_prefix_when_base_is_subdir(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    out = GrepTool._format_rg_output("./a.py:1:x\n", sub, _ctx(tmp_path, working_dir=tmp_path))
    assert out == "sub/a.py:1:x"


def test_format_rg_output_omitted_and_stopped_notes(tmp_path):
    ctx = _ctx(tmp_path)
    many_in_file = "\n".join(f"./big.py:{i}:hit" for i in range(1, MAX_PER_FILE + 5))
    rows = GrepTool._format_rg_output(many_in_file, tmp_path, ctx).splitlines()
    assert any("more matches omitted" in r for r in rows)

    many_files = "\n".join(f"./f{i}.py:1:hit" for i in range(MAX_MATCHES + 100))
    out2 = GrepTool._format_rg_output(many_files, tmp_path, ctx)
    assert out2.splitlines()[-1] == f"... stopped at {MAX_MATCHES} matches"


def test_format_rg_output_empty_and_garbage(tmp_path):
    ctx = _ctx(tmp_path)
    assert GrepTool._format_rg_output("", tmp_path, ctx) == "(no matches)"
    assert GrepTool._format_rg_output("garbage\n", tmp_path, ctx) == "(no matches)"


# ---- _scan_rg：命令构造与回退（假 rg，不依赖本机装没装） ----


def test_scan_rg_returns_none_without_rg(tmp_path, monkeypatch):
    monkeypatch.setattr("skysheep.tools.search.shutil.which", lambda _name: None)
    assert GrepTool._scan_rg("x", tmp_path, _ctx(tmp_path), "") is None


def test_scan_rg_builds_command_and_formats(tmp_path, monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "./a.py:1:hit\n"

    monkeypatch.setattr("skysheep.tools.search.shutil.which", lambda _name: "rg.exe")
    monkeypatch.setattr(
        "skysheep.tools.search.subprocess.run",
        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or _Proc(),
    )
    out = GrepTool._scan_rg("needle", tmp_path, _ctx(tmp_path), "*.py")
    assert out == "a.py:1:hit"
    cmd = seen["cmd"]
    assert cmd[0] == "rg.exe"
    assert "-e" in cmd and "needle" in cmd
    assert "*.py" in cmd and "--glob" in cmd
    assert seen["kw"]["cwd"] == str(tmp_path)


def test_scan_rg_file_base_uses_parent_and_name(tmp_path, monkeypatch):
    f = tmp_path / "one.txt"
    f.write_text("x", encoding="utf-8")
    seen = {}

    class _Proc:
        returncode = 1  # 无匹配也算成功
        stdout = ""

    monkeypatch.setattr("skysheep.tools.search.shutil.which", lambda _name: "rg.exe")
    monkeypatch.setattr(
        "skysheep.tools.search.subprocess.run",
        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or _Proc(),
    )
    out = GrepTool._scan_rg("x", f, _ctx(tmp_path), "")
    assert out == "(no matches)"
    assert seen["kw"]["cwd"] == str(tmp_path)
    assert seen["cmd"][-1] == "one.txt"


def test_scan_rg_adds_ignore_file_when_present(tmp_path, monkeypatch):
    (tmp_path / ".skysheepignore").write_text("secret.txt\n", encoding="utf-8")
    seen = {}

    class _Proc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr("skysheep.tools.search.shutil.which", lambda _name: "rg.exe")
    monkeypatch.setattr(
        "skysheep.tools.search.subprocess.run",
        lambda cmd, **kw: seen.update(cmd=cmd) or _Proc(),
    )
    GrepTool._scan_rg("x", tmp_path, _ctx(tmp_path), "")
    assert "--ignore-file" in seen["cmd"]


def test_scan_rg_exit2_oserror_and_timeout_fall_back(tmp_path, monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr("skysheep.tools.search.shutil.which", lambda _name: "rg.exe")

    class _Proc:
        returncode = 2  # rg 正则解析失败等
        stdout = "boom"

    monkeypatch.setattr("skysheep.tools.search.subprocess.run", lambda cmd, **kw: _Proc())
    assert GrepTool._scan_rg("x", tmp_path, _ctx(tmp_path), "") is None

    def _oserr(cmd, **kw):
        raise OSError("no exec")

    monkeypatch.setattr("skysheep.tools.search.subprocess.run", _oserr)
    assert GrepTool._scan_rg("x", tmp_path, _ctx(tmp_path), "") is None

    def _timeout(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, 1)

    monkeypatch.setattr("skysheep.tools.search.subprocess.run", _timeout)
    assert GrepTool._scan_rg("x", tmp_path, _ctx(tmp_path), "") is None


# ---- run / 内置 _scan 的边界分支 ----


def test_grep_run_path_not_found(tmp_path):
    with pytest.raises(ToolError):
        asyncio.run(GrepTool().run(GrepArgs(pattern="x", path="nope"), _ctx(tmp_path)))


def test_grep_run_invalid_regex(tmp_path):
    with pytest.raises(ToolError):
        asyncio.run(GrepTool().run(GrepArgs(pattern="[", path="."), _ctx(tmp_path)))


def test_grep_run_uses_rg_result_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(GrepTool, "_scan_rg", staticmethod(lambda *a, **k: "rg-result"))
    out = asyncio.run(
        GrepTool().run(GrepArgs(pattern="ascii", path="."), _ctx(tmp_path, use_ripgrep=True))
    )
    assert out == "rg-result"


def test_grep_run_falls_back_when_rg_returns_none(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("ascii hit\n", encoding="utf-8")
    monkeypatch.setattr(GrepTool, "_scan_rg", staticmethod(lambda *a, **k: None))
    out = asyncio.run(
        GrepTool().run(GrepArgs(pattern="ascii", path="."), _ctx(tmp_path, use_ripgrep=True))
    )
    assert "a.txt:1:ascii hit" in out


def test_grep_run_ignore_load_failure_falls_back(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hit\n", encoding="utf-8")

    def _boom(cls, _root):
        raise OSError("no ignore")

    monkeypatch.setattr("skysheep.core.ignore.IgnoreRules.load", classmethod(_boom))
    out = asyncio.run(GrepTool().run(GrepArgs(pattern="hit", path="."), _ctx(tmp_path)))
    assert "a.txt:1:hit" in out


def test_scan_include_binary_and_oversize(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("hit here\n", encoding="utf-8")
    (tmp_path / "b.png").write_bytes(b"hit binary\n")  # 二进制后缀：跳过
    (tmp_path / "weird.dat").write_bytes(b"\x00\x01\x02hit")  # 后缀正常但内容像二进制：跳过
    out = GrepTool._scan(re.compile("hit"), tmp_path, _ctx(tmp_path), None, "*.py")
    assert out == "a.py:1:hit here"

    # 不带 include：走到「内容像二进制」分支（decode_bytes 判 binary → 跳过）
    out_all = GrepTool._scan(re.compile("hit"), tmp_path, _ctx(tmp_path), None, "")
    assert out_all.splitlines() == ["a.py:1:hit here"]

    monkeypatch.setattr("skysheep.tools.search.MAX_FILE_SIZE", 1)
    assert GrepTool._scan(re.compile("hit"), tmp_path, _ctx(tmp_path), None, "") == "(no matches)"


def test_scan_skips_unreadable_file(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hit\n", encoding="utf-8")
    real = Path.read_bytes

    def _boom(self):
        if self.name == "a.txt":
            raise OSError("permission")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    out = GrepTool._scan(re.compile("hit"), tmp_path, _ctx(tmp_path), None, "")
    assert out == "(no matches)"


def test_scan_stops_at_max_matches_and_per_file_note(tmp_path, monkeypatch):
    monkeypatch.setattr("skysheep.tools.search.MAX_MATCHES", 3)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("hit\n", encoding="utf-8")
    rows = GrepTool._scan(re.compile("hit"), tmp_path, _ctx(tmp_path), None, "").splitlines()
    assert rows[-1] == "... stopped at 3 matches"

    monkeypatch.setattr("skysheep.tools.search.MAX_MATCHES", 200)
    monkeypatch.setattr("skysheep.tools.search.MAX_PER_FILE", 2)
    (tmp_path / "many.txt").write_text("hit\nhit\nhit\nhit\n", encoding="utf-8")
    rows2 = GrepTool._scan(re.compile("hit"), tmp_path, _ctx(tmp_path), None, "").splitlines()
    assert any("more matches omitted" in r for r in rows2)


def test_scan_file_base_yields_itself(tmp_path):
    f = tmp_path / "one.txt"
    f.write_text("hit\n", encoding="utf-8")
    out = GrepTool._scan(re.compile("hit"), f, _ctx(tmp_path), None, "")
    assert out == "one.txt:1:hit"
