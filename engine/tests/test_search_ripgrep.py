"""grep 工具的 ripgrep 可选加速：rg/内置两路结果对齐、回退与上限。

加速只在「use_ripgrep 开 + 本机有 rg + 模式纯 ASCII」时启用；本组用例
同时验证 rg 路径与内置路径输出一致（排序后逐行相等），以及缺 rg、rg
执行失败、非 ASCII 模式时都回退内置实现。
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from skysheep.tools.base import ToolContext
from skysheep.tools.search import GrepArgs, GrepTool

RG_AVAILABLE = shutil.which("rg") is not None

# 纯 ASCII、词形独特：避免撞上夹具树里的其他内容
TOKEN = "NEEDLE_XYZ_42"


def _ctx(tmp_path, use_ripgrep: bool = True) -> ToolContext:
    return ToolContext(working_dir=tmp_path, use_ripgrep=use_ripgrep)


def _run(tmp_path, pattern: str = TOKEN, use_ripgrep: bool = True) -> str:
    ctx = _ctx(tmp_path, use_ripgrep=use_ripgrep)
    return asyncio.run(GrepTool().run(GrepArgs(pattern=pattern, path="."), ctx))


def _norm(out: str) -> list[str]:
    """排序后的行列表；路径分隔符归一成 / 再比（内置实现在 Windows 上
    经 rel_path 输出反斜杠，rg 路径按设计输出正斜杠，属已知的外观差异）。"""
    return sorted(line.replace("\\", "/") for line in out.splitlines())


@pytest.fixture
def tree(tmp_path):
    """共享夹具树：rg 与内置两路必须给出同一批结果（含「都必须搜不到」的干扰项）。"""
    (tmp_path / "app.py").write_text(
        f"def main():\n    return '{TOKEN}'\n", encoding="utf-8"
    )
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_text(f"x = {TOKEN}  # nested\n", encoding="utf-8")
    # GBK 文件：命中行保持纯 ASCII（rg 按 UTF-8 输出该行与内置解码后一致），
    # 另一行中文不含 token
    (tmp_path / "gbk.txt").write_bytes(
        f"plain line with {TOKEN}\n中文注释：没有标记的一行\n".encode("gbk")
    )
    # 干扰项：node_modules 剪枝、内建忽略 .env、startswith(".git") 剪枝
    # .github、.skysheepignore 忽略 secret.txt——两路实现都必须搜不到
    noise = tmp_path / "node_modules"
    noise.mkdir()
    (noise / "pkg.js").write_text(f"{TOKEN} in node_modules\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"KEY={TOKEN}\n", encoding="utf-8")
    gh = tmp_path / ".github" / "workflows"
    gh.mkdir(parents=True)
    (gh / "ci.yml").write_text(f"run: echo {TOKEN}\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(f"{TOKEN} secret\n", encoding="utf-8")
    (tmp_path / ".skysheepignore").write_text("secret.txt\n", encoding="utf-8")
    # 超 300 字符长行：两路实现都截断到 300
    (tmp_path / "longline.md").write_text(
        "start " + TOKEN + " " + "a" * 400 + "\n", encoding="utf-8"
    )
    # 不含 token 的对照文件
    (tmp_path / "empty.txt").write_text("nothing here\n", encoding="utf-8")
    return tmp_path


@pytest.mark.skipif(not RG_AVAILABLE, reason="本机无 rg")
def test_rg_results_match_builtin(tree):
    """rg 路径与内置路径对同一棵树给出完全一致的结果（排序后逐行相等）。"""
    rg_out = _run(tree, use_ripgrep=True)
    py_out = _run(tree, use_ripgrep=False)
    assert rg_out != "(no matches)"
    assert _norm(rg_out) == _norm(py_out)
    # 命中：utf-8 代码、GBK 文件的 ASCII 行、子目录、长行（内容截到 300）
    assert "app.py:2" in rg_out
    assert "gbk.txt:1" in rg_out
    assert "sub/inner.py:1" in rg_out
    assert rg_out.splitlines().count("longline.md:1:" + "start " + TOKEN + " " + "a" * 280) == 1
    # 干扰项两路都搜不到：隐藏目录（.github）、内建忽略（.env）、
    # .skysheepignore（secret.txt）、node_modules
    for absent in (".github", "node_modules", "secret.txt"):
        assert absent not in rg_out
    assert ".env" not in rg_out


def test_fallback_when_rg_missing(tree, monkeypatch):
    """rg 不在 PATH（shutil.which → None）时回退内置，结果仍正确。"""
    monkeypatch.setattr("skysheep.tools.search.shutil.which", lambda name: None)
    out = _run(tree, use_ripgrep=True)
    py_out = _run(tree, use_ripgrep=False)
    assert _norm(out) == _norm(py_out)
    assert "app.py:2" in out


def test_non_ascii_pattern_skips_rg(tree, monkeypatch):
    """中文模式不进 rg 路径（保留 textio 的 GB18030 解码语义），走内置实现。"""

    def _boom(*args, **kwargs):
        raise AssertionError("非 ASCII 模式不应进入 rg 路径")

    monkeypatch.setattr(GrepTool, "_scan_rg", staticmethod(_boom))
    out = asyncio.run(
        GrepTool().run(GrepArgs(pattern="中文注释", path="."), _ctx(tree))
    )
    assert "gbk.txt:2" in out, out


def test_leading_dash_pattern(tmp_path):
    """前导连字符的模式经 -e 传入 rg，不会被吃成命令行参数。"""
    (tmp_path / "dash.txt").write_text(f"keep {TOKEN}-dash here\n", encoding="utf-8")
    out = asyncio.run(
        GrepTool().run(GrepArgs(pattern="-dash", path="."), _ctx(tmp_path))
    )
    assert f"dash.txt:1:keep {TOKEN}-dash here" in out, out


def test_rg_error_falls_back(tree, monkeypatch):
    """subprocess.run 抛 OSError（rg 执行失败）时回退内置，结果仍正确。"""
    import skysheep.tools.search as search_mod

    monkeypatch.setattr("skysheep.tools.search.shutil.which", lambda name: "rg")

    def _raise(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(search_mod.subprocess, "run", _raise)
    out = _run(tree, use_ripgrep=True)
    py_out = _run(tree, use_ripgrep=False)
    assert _norm(out) == _norm(py_out)
    assert "sub/inner.py:1" in out.replace("\\", "/")


def test_per_file_cap_and_omitted_note(tmp_path):
    """单文件 25 行同 token：只出 20 行 + 一次 omitted 注记（rg/内置一致）。"""
    lines = "\n".join(f"line {i} {TOKEN}" for i in range(25)) + "\n"
    (tmp_path / "many.txt").write_text(lines, encoding="utf-8")
    out = _run(tmp_path, use_ripgrep=True)
    out_lines = out.splitlines()
    matches = [
        ln for ln in out_lines
        if ln.startswith("many.txt:") and "omitted" not in ln
    ]
    notes = [ln for ln in out_lines if "more matches omitted" in ln]
    assert len(matches) == 20
    assert notes == ["many.txt: ... more matches omitted"]
    # rg 路径与内置路径在这个场景下也一致
    assert _norm(out) == _norm(_run(tmp_path, use_ripgrep=False))
