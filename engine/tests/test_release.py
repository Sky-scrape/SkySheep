"""发布脚本的门禁回归测试。

这些测试只覆盖纯逻辑和 fail-closed 分支，不启动 PyInstaller、ISCC 或 GitHub CLI，
避免测试修改真实安装包或产生对外发布副作用。
"""

import argparse
import importlib.util
import inspect
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
    for flag in ("--skip-pack", "--skip-smoke", "--skip-installer", "--skip-quality"):
        with pytest.raises(SystemExit):
            release.parse_args(["--upload", flag])


def test_build_steps_runs_quality_before_pack():
    args = argparse.Namespace(
        skip_pack=False,
        skip_smoke=False,
        skip_installer=False,
        skip_quality=False,
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


def test_quality_gate_is_skippable_for_ci():
    """§2 质量门可跳过：CI 的 test job 已在同一轮跑过它，发布候选 job 不必重跑一遍。"""
    args = argparse.Namespace(
        skip_pack=False, skip_smoke=False, skip_installer=False, skip_quality=True, upload=False
    )
    quality = next(s for s in release.build_steps(args) if s.title.startswith("发布质量门"))
    assert quality.skip_flag == "skip_quality"


def test_iscc_honors_env_override(monkeypatch, tmp_path):
    """SKYSHEEP_ISCC 指哪就是哪（CI 上 choco 装的 Inno Setup 走这条）。"""
    fake = tmp_path / "ISCC.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("SKYSHEEP_ISCC", str(fake))
    assert release.iscc_path() == fake


def test_iscc_falls_back_to_candidates_then_none(monkeypatch, tmp_path):
    """没给环境变量时按候选位探测；候选位都没有返回 None（由调用方报出可执行的提示）。"""
    monkeypatch.delenv("SKYSHEEP_ISCC", raising=False)
    fake = tmp_path / "ISCC.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(release, "_ISCC_CANDIDATES", (tmp_path / "missing.exe", fake))
    assert release.iscc_path() == fake
    monkeypatch.setattr(release, "_ISCC_CANDIDATES", (tmp_path / "missing.exe",))
    assert release.iscc_path() is None


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


def test_quality_gate_matches_ci_coverage_threshold():
    """发布质量门与 CI 同口径：单测带覆盖率门槛，不再跑一套更松的门禁。"""
    src = inspect.getsource(release.step_quality)
    assert "--cov-fail-under=80.5" in src, "本地发布必须与 CI 同覆盖率门槛"
    assert "--cov=skysheep" in src
    # 三个标记都要跑到：单测（含覆盖率）+ e2e + eval
    assert '"not e2e and not eval"' in src
    assert '"e2e"' in src and '"eval"' in src


def test_ci_workflow_has_release_candidate_job():
    """CI 必须存在发布候选门禁，且必须跑共享的 release.py 流水线。

    只跑源码单测的 CI 不能证明发布包可构建、可启动；这条钉住发布候选 job 不被误删，
    也钉住「不许再把冒烟手写一遍」——2026-10-12 之前它正是手写 PowerShell 版：
    $home 撞上只读的 $HOME 让脚本第 2 行即死，从引入（2026-10-11）到 v2.5.1 首次
    执行一直是红的，等于没有门禁。隔离口径与端口解析只保留在 release.py 里。
    """
    repo_root = Path(__file__).resolve().parents[2]
    ci = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "package:" in ci, "缺少发布候选 job"
    assert "python.exe tools\\release.py --skip-quality" in ci, "发布候选必须复用 release.py 流水线"
    assert "SKYSHEEP_ISCC" in ci, "CI 上必须显式喂 ISCC 位置，别依赖只有本机才有的固定路径"
    assert "ChineseSimplified.isl" in ci, "installer.iss 要的中文消息文件必须在 CI 上就位（IS6 不带它）"
    assert "workflow_dispatch" in ci
    # 每个 job 都要有超时保护（防止 teardown 挂起拖死流水线）
    assert ci.count("timeout-minutes:") >= 4
    # 隔离冒烟仍在，且只有一份实现：隔离 home / 独立身份 / 端口从日志解析
    smoke = inspect.getsource(release.step_smoke)
    assert "SKYSHEEP_HOME" in smoke and "SKYSHEEP_INSTANCE" in smoke
    assert "_URL_IN_LOG_RE" in inspect.getsource(release._wait_ready), "端口必须从日志解析，不能写死"
