"""``check_changelog_coverage.py`` 的用例：发布前「提交是否进过 CHANGELOG」报告。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location(
    "check_changelog_coverage", _TOOLS / "check_changelog_coverage.py"
)
assert _SPEC and _SPEC.loader
cc = importlib.util.module_from_spec(_SPEC)
# dataclass 在 Python 3.12 会按 cls.__module__ 回查 sys.modules；不登记会
# 在 exec_module 时抛 AttributeError: 'NoneType' object has no attribute '__dict__'
sys.modules[_SPEC.name] = cc
_SPEC.loader.exec_module(cc)


def test_release_subject_filter():
    assert cc.is_release_subject("feat: 新功能")
    assert cc.is_release_subject("feat(引擎/UI): 新功能")
    assert cc.is_release_subject("fix(security): 修复")
    assert cc.is_release_subject("perf: 提速")
    assert not cc.is_release_subject("docs: 文档")
    assert not cc.is_release_subject("chore: 杂务")
    assert not cc.is_release_subject("test: 用例")
    assert not cc.is_release_subject("feature: 不是约定前缀")
    assert not cc.is_release_subject("fixed: 不是约定前缀")


def test_parse_log_keeps_tabs_inside_subject():
    out = "abc\t2026-11-18\tfix: 前半\t后半\n\n"
    assert cc.parse_log(out) == [("abc", "2026-11-18", "fix: 前半\t后半")]


def test_previous_tag_ignores_current_version(monkeypatch):
    monkeypatch.setattr(
        cc, "_git", lambda *args, **kwargs: "v2.7.1\nv2.7.0\nv2.6.9\nnot-a-tag\n"
    )
    assert cc.previous_tag("2.7.1", Path(".")) == "v2.7.0"
    assert cc.previous_tag(None, Path(".")) == "v2.7.1"


def test_format_report_lists_only_missing_by_default():
    commits = [
        cc.Commit("a" * 9, "2026-11-18", "fix: 还没记", False),
        cc.Commit("b" * 9, "2026-11-18", "feat: 已记", True),
    ]
    report = cc.format_report("v2.7.1", 10, commits)
    assert "fix: 还没记" in report
    assert "feat: 已记" not in report
    assert "1 个 feat/fix/perf 提交没有在本提交里改 CHANGELOG.md" in report


def test_format_report_clean_and_all_view():
    commits = [cc.Commit("a" * 9, "2026-11-18", "fix: 已记", True)]
    assert "全部 feat/fix/perf 提交都触碰过 CHANGELOG.md" in cc.format_report(
        "v2.7.1", 3, commits
    )
    assert "fix: 已记" in cc.format_report("v2.7.1", 3, commits, show_all=True)


def test_collect_marks_only_relevant_commits(monkeypatch):
    def fake_git(*args, **kwargs):
        if args[0] == "log":
            return (
                "a\t2026-11-18\tfix: 有记录\n"
                "b\t2026-11-18\tdocs: 不重要\n"
                "c\t2026-11-18\tfeat: 没记录\n"
            )
        if args[0] == "show":
            sha = args[3]
            return "CHANGELOG.md\n" if sha == "a" else ""
        raise AssertionError(args)

    monkeypatch.setattr(cc, "_git", fake_git)
    total, commits = cc.collect("v2.7.1", Path("."))
    assert total == 3
    assert [c.subject for c in commits] == ["fix: 有记录", "feat: 没记录"]
    assert commits[0].touched_changelog is True
    assert commits[1].touched_changelog is False
