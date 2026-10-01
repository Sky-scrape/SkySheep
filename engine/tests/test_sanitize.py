"""sanitize.sanitize_description 共享实现的直接锚定（2026-10 审查项 11）。

MCP 工具描述与技能描述的防注入形状约束此前是两份逐字拷贝、仅靠注释约定同步
（收敛前 skills 侧的分叉风险只有公开路径的间接兜底）。收敛为单一实现后，这里
直接锚定共享函数本身，并锁两端调用点的对齐：将来新增形状约束（如零宽字符
过滤）改 sanitize 模块即两端同时生效；若有人把某个包装层改回本地实现、或两端
行为漂移，下面的对齐断言会红。
"""

from skysheep.mcp.client import (
    MAX_MCP_DESCRIPTION_CHARS,
)
from skysheep.mcp.client import (
    _sanitize_description as mcp_sanitize,
)
from skysheep.sanitize import (
    EMPTY_DESCRIPTION_PLACEHOLDER,
    MAX_PROMPT_DESCRIPTION_CHARS,
    sanitize_description,
)
from skysheep.skills.loader import (
    MAX_SKILL_DESCRIPTION_CHARS,
)
from skysheep.skills.loader import (
    _sanitize_description as skill_sanitize,
)


def test_constants_are_shared_and_pinned():
    """限长与占位符共用一份：两端别名必须指向同一常量，值不漂移。"""
    assert MAX_PROMPT_DESCRIPTION_CHARS == 1000
    assert MAX_MCP_DESCRIPTION_CHARS is MAX_PROMPT_DESCRIPTION_CHARS
    assert MAX_SKILL_DESCRIPTION_CHARS is MAX_PROMPT_DESCRIPTION_CHARS
    assert EMPTY_DESCRIPTION_PLACEHOLDER == "(no description)"


def test_blank_text_fallback_differs_by_side():
    """空文本回退差异按现状保留：MCP 侧占位符，技能侧空串（渲染层兜底）。"""
    assert sanitize_description("") == ""
    assert sanitize_description("  \n ") == ""
    assert sanitize_description(None) == ""  # (text or "") 容忍 None
    assert (
        sanitize_description("", empty_fallback=EMPTY_DESCRIPTION_PLACEHOLDER)
        == "(no description)"
    )
    assert mcp_sanitize("") == "(no description)"
    assert skill_sanitize("") == ""


def test_shape_constraints_are_shared():
    """限长 / 截断后缀 / 空行压缩：两端包装对同一输入产出完全一致。"""
    long_text = "x" * (MAX_PROMPT_DESCRIPTION_CHARS + 500)
    blank_heavy = "a\n\n\n\nb   \n\nc"
    for sample in (long_text, blank_heavy, "plain"):
        shared = sanitize_description(sample)
        assert mcp_sanitize(sample) == shared
        assert skill_sanitize(sample) == shared
    # 截断：上限 + 后缀，后缀表明描述不全
    out = sanitize_description(long_text)
    assert len(out) == MAX_PROMPT_DESCRIPTION_CHARS + len(" …（描述过长已截断）")
    assert out.endswith("…（描述过长已截断）")
    # 连续空行压成一个、行尾空白收掉，避免用空白把内容推到看不见的位置
    assert sanitize_description(blank_heavy) == "a\n\nb\n\nc"
