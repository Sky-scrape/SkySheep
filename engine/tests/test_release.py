"""发布脚本的门禁回归测试。

这些测试只覆盖纯逻辑和 fail-closed 分支，不启动 PyInstaller、ISCC 或 GitHub CLI，
避免测试修改真实安装包或产生对外发布副作用。
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location("release_tool", _TOOLS / "release.py")
assert _SPEC and _SPEC.loader
release = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = release
_SPEC.loader.exec_module(release)


def test_parse_args_rejects_partial_upload():
    """上传不能复用旧 dist 或旧安装包，必须走完整构建与验证。"""
    for flag in ("--skip-pack", "--skip-smoke", "--skip-installer"):
        with pytest.raises(SystemExit):
            release.parse_args(["--upload", flag])


def test_build_steps_runs_quality_before_pack():
    args = argparse.Namespace(
        skip_pack=False,
        skip_smoke=False,
        skip_installer=False,
        upload=False,
    )
    steps = release.build_steps(args)
    titles = [step.title for step in steps]
    assert titles[:3] == [
        "完整版本一致性预检",
        "依赖同步 uv sync --locked",
        "发布质量门（lint / unit / e2e / eval）",
    ]
    assert titles.index("发布质量门（lint / unit / e2e / eval）") < titles.index(
        "PyInstaller 打包（不经 uv run）"
    )


def test_pyproject_version_requires_three_part_semver(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "2.4"\n', encoding="utf-8"
    )
    monkeypatch.setattr(release, "_ENGINE_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="三段式 semver"):
        release.pyproject_version()


def test_upload_rejects_stale_sha_before_external_tools(monkeypatch, tmp_path):
    installer = tmp_path / "SkySheep-2.4.1-setup.exe"
    installer.write_bytes(b"new installer")
    sha = installer.with_name(installer.name + ".sha256")
    sha.write_text("0" * 64 + "  " + installer.name + "\n", encoding="utf-8")
    monkeypatch.setattr(release, "_ENGINE_DIR", tmp_path)
    monkeypatch.setattr(release, "_INSTALLER_DIR", tmp_path)
    monkeypatch.setattr(release, "_REPO_ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "2.4.1"\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="不匹配"):
        release.step_upload(True)
