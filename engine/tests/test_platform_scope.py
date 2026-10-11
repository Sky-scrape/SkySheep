"""平台支持口径的机械守卫：Windows 是唯一受支持平台，跨平台不是近期承诺。

2026-10-11 定调：SkySheep 的安装包、桌面壳与安全模型（DPAPI 凭据加密、
Windows 沙箱、窗口与进程管理、`schtasks` 定时导出）都是 Windows 实现，
`--browser` 只是开发者兜底路径。此前 README / CONTRIBUTING / 落地页把
「macOS / Linux 在路线图上」写成后续方向，会持续吸引「什么时候支持 Mac」
的期望与 PR；这里把口径锁成一条，防止文案回潮。

只扫用户可见的平台承诺面；`docs/发布清单.md` 不参与扫描——它把被禁句式
当作反面样例列在自查项里，扫它必然误报。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# 用户可见的平台承诺面：README 中英、贡献指南、安全策略、落地页、平台支持说明
_SCOPE_FILES = (
    "README.md",
    "README.en.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "index.html",
    "docs/版本支持与更新渠道.md",
)

# 禁止回潮的句式：把跨平台写成「优先之后的下一个目标」或「兜底即支持」
_FORBIDDEN = (
    r"Windows\s*优先",
    r"Windows-first",
    r"macOS\s*/\s*Linux[^。\n]{0,20}在路线图上",
    r"macOS\s*/\s*Linux\s+(?:support|on the roadmap)",
    r"on the roadmap[^.\n]{0,20}macOS",
    r"跨平台兜底",
    r"cross-platform fallback",
)

# 必须出现的一句定调（中文面与英文面各一），防止只删不补把边界说糊
_REQUIRED = {
    "README.md": "Windows 桌面产品",
    "README.en.md": "Windows desktop product",
}


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", _SCOPE_FILES)
@pytest.mark.parametrize("pattern", _FORBIDDEN)
def test_no_cross_platform_promise_wording(rel: str, pattern: str):
    text = _read(rel)
    m = re.search(pattern, text)
    assert m is None, (
        f"{rel} 出现被禁的跨平台承诺句式 {pattern!r}：{m.group(0)!r}\n"
        "口径应为「Windows 是唯一受支持平台，--browser 是开发者兜底、不在支持承诺内，"
        "跨平台待 Windows 稳定后再评估」"
    )


@pytest.mark.parametrize("rel,phrase", sorted(_REQUIRED.items()))
def test_platform_boundary_stated(rel: str, phrase: str):
    assert phrase in _read(rel), f"{rel} 缺少平台边界定调：{phrase!r}"


def test_landing_page_chip_does_not_say_windows_first():
    """落地页首屏徽记写死产品形态，不再用「优先」这种可被读成暂时性的措辞。"""
    html = _read("index.html")
    assert "Windows 桌面应用" in html
    assert "Windows 优先" not in html


def test_roadmap_next_up_does_not_list_cross_platform():
    """两份 README 的「后续方向 / Next up」不得把跨平台列为待做项。"""
    for rel, marker in (("README.md", "🚧 **后续方向**"), ("README.en.md", "🚧 **Next up**")):
        line = next((ln for ln in _read(rel).splitlines() if marker in ln), "")
        assert line, f"{rel} 找不到「{marker}」行"
        assert "macOS" not in line and "Linux" not in line, f"{rel} 后续方向仍列跨平台：{line}"
        assert "代码签名分发" in line or "code-signed distribution" in line
