"""界面术语、主题色板和常用控件的一致性护栏。"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/skysheep/server/static"


@pytest.mark.parametrize("theme", ["paper", "night", "celadon", "kaki", "indigo", "pine"])
def test_theme_has_accent_foreground_and_chart_palette(theme):
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    match = re.search(rf'\[data-theme="{theme}"\]\s*\{{([^}}]+)\}}', css)
    assert match
    for token in ["--on-accent", *(f"--dm-c{i}" for i in range(1, 9))]:
        assert re.search(rf"{token}\s*:", match.group(1)), (theme, token)


def test_legacy_color_tokens_are_not_used():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for token in ("paper", "accent", "accent-weak", "danger", "line-1", "text-dim",
                  "warn", "warn-bg", "card-alt", "amber"):
        assert not re.search(rf"var\(--{token}\s*[,)]", css), token


def test_project_items_and_session_actions_use_consistent_names():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    project = (STATIC / "app-projects.js").read_text(encoding="utf-8")
    for src in (js, project):
        assert "新建对话" not in src
    start = js.index("function renderProjectTasks(")
    end = js.index("function setRunning(", start)
    items = js[start:end]
    assert '"新建事项" : "编辑事项"' in items
    for verb in ("更新", "删除", "添加"):
        assert f"{verb}事项失败" in items


def test_relay_help_does_not_describe_retired_report_ui():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    start = js.index("function showHelp()")
    end = js.index('document.getElementById("btn-help").onclick', start)
    help_text = js[start:end]
    assert "主屏与副屏" in help_text
    assert "A/B 是模型槽位" not in help_text
    assert "当前不使用工具" not in help_text
