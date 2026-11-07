"""Release 正文自检的回归测试。

覆盖「机械能拦住的东西」：版本号、安装包名、sha256、图片、SmartScreen 链接与
句子中间的手动换行。发布脚本的上传分支本身不在这里跑（会碰 gh / git）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location("check_release_notes_tool", _TOOLS / "check_release_notes.py")
assert _SPEC and _SPEC.loader
checker = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = checker
_SPEC.loader.exec_module(checker)

_SHA = "f0c1e7189438c86276647de6a4e3ebfaeb0cc5f697270e53ddf5eeea897c0f9c"

_GOOD = f"""# SkySheep v2.7.0 · 协作有了模板

![SkySheep](https://sheepsky.com/docs/images/banner.png)

一句话导语。

## 这版你能感觉到什么

- **协作收成模板**：三种形态一套模板，复制一份就能改。

## 怎么装

Windows 10 / 11 x64。安装包 `SkySheep-2.7.0-setup.exe`，未签名。
首次运行如遇 SmartScreen：见 [说明](https://example.com/smartscreen-说明.md)。
sha256：`{_SHA}`

<details>
<summary>工程细节与边界（1 条）</summary>

- 一条细节。

</details>
"""


def test_good_notes_pass():
    assert checker.check_text(_GOOD, None) == []


def test_missing_image_is_rejected():
    text = _GOOD.replace("![SkySheep](https://sheepsky.com/docs/images/banner.png)\n\n", "")
    assert any("图片" in err for err in checker.check_text(text, None))


def test_wrong_installer_name_is_rejected():
    text = _GOOD.replace("SkySheep-2.7.0-setup.exe", "SkySheep-2.6.9-setup.exe")
    assert any("安装包名" in err for err in checker.check_text(text, None))


def test_missing_sha256_is_rejected():
    text = _GOOD.replace(_SHA, "待填")
    assert any("sha256" in err for err in checker.check_text(text, None))


def test_missing_smartscreen_link_is_rejected():
    text = _GOOD.replace("SmartScreen", "签名提示").replace("smartscreen-说明.md", "x.md")
    assert any("SmartScreen" in err for err in checker.check_text(text, None))


def test_version_argument_must_match_title():
    assert any("v2.7.1" in err for err in checker.check_text(_GOOD, "2.7.1"))


def test_hard_wrapped_sentence_is_rejected():
    text = _GOOD.replace(
        "- **协作收成模板**：三种形态一套模板，复制一份就能改。",
        "- **协作收成模板**：三种形态一套模板，\n  复制一份就能改。",
    )
    assert any("手动换行" in err for err in checker.check_text(text, None))


def test_list_items_are_not_reported_as_wraps():
    assert checker.find_hard_wraps("- 第一条\n- 第二条\n- 第三条") == []


def test_table_and_code_fence_are_ignored():
    text = "| 列一 | 列二 |\n| --- | --- |\n| 值 | 值 |\n\n```\n这不是正文\n```\n"
    assert checker.find_hard_wraps(text) == []


@pytest.mark.parametrize("name", ["TEMPLATE-feature.md", "TEMPLATE-fix.md"])
def test_templates_pass_template_mode(name):
    path = Path(__file__).resolve().parents[2] / "docs" / "release-notes" / name
    errors = checker.check_text(path.read_text(encoding="utf-8"), None, template=True)
    assert errors == []


def test_release_notes_path_uses_version_file(tmp_path, monkeypatch):
    tools_spec = importlib.util.spec_from_file_location("release_tool_notes", _TOOLS / "release.py")
    assert tools_spec and tools_spec.loader
    release = importlib.util.module_from_spec(tools_spec)
    sys.modules[tools_spec.name] = release
    tools_spec.loader.exec_module(release)

    notes_dir = tmp_path / "docs" / "release-notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "2.7.0.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.setattr(release, "_RELEASE_NOTES_DIR", notes_dir)

    assert release.release_notes_path("2.7.0") == notes_dir / "2.7.0.md"
    assert release.release_notes_path("2.7.1") is None
    with pytest.raises(RuntimeError, match="不存在"):
        release.release_notes_path("2.7.0", str(tmp_path / "missing.md"))
