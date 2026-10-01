"""外部内容进系统提示词前的形状约束（共享实现）。

MCP 工具描述与技能名/描述都会原样拼进模型可见的提示词面；来源（MCP 服务器、
技能包 frontmatter）都是外部内容，超长会挤占上下文，大量空白能把注入内容推到
看不见的位置。限长 + 压空白的处理此前在 mcp/client.py 与 skills/loader.py 各存
一份拷贝、仅靠注释约定同步——现收敛到这里，将来发现新绕过（如零宽字符）只改
一处、两个入口同时生效。只做形状约束（长度、空白），不尝试识别「恶意指令」
——那种判断不适合放在按长度/格式的过滤里，会既漏又误伤；真正的边界是来源
是否可信（workspace trust / 技能来源）。
"""

from __future__ import annotations

# 描述限长：MCP 工具描述与技能描述共用一份（两个调用方各留旧名作别名）。
MAX_PROMPT_DESCRIPTION_CHARS = 1000

# 空描述的占位符：MCP 工具目录里空描述以它示人；技能清单渲染层同用它兜底。
EMPTY_DESCRIPTION_PLACEHOLDER = "(no description)"

_TRUNCATION_SUFFIX = " …（描述过长已截断）"


def sanitize_description(text: str, *, empty_fallback: str = "") -> str:
    """描述文本的展示清理：限长 + 压掉多余空白行；空文本按调用方语义回退。

    - MCP 工具描述传 empty_fallback=EMPTY_DESCRIPTION_PLACEHOLDER（工具目录
      里不留空串）；
    - 技能描述用默认空串（渲染层再用 EMPTY_DESCRIPTION_PLACEHOLDER 兜底，
      加载层不制造占位文本）。

    截断附一句提示，便于看出描述不全；连续空行压成一个，避免用大量空行把
    内容推到看不见的位置。
    """
    raw = (text or "").strip()
    if not raw:
        return empty_fallback
    # 连续空行压成一个，避免用大量空行把注入内容推到看不见的位置
    lines = [ln.rstrip() for ln in raw.splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if not ln:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(ln)
    text_out = "\n".join(out).strip()
    if len(text_out) > MAX_PROMPT_DESCRIPTION_CHARS:
        text_out = text_out[:MAX_PROMPT_DESCRIPTION_CHARS] + _TRUNCATION_SUFFIX
    return text_out


__all__ = [
    "EMPTY_DESCRIPTION_PLACEHOLDER",
    "MAX_PROMPT_DESCRIPTION_CHARS",
    "sanitize_description",
]
