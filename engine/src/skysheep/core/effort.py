"""思考强度「自动」档：按任务复杂度实时估算 low / medium / high。

用户把思考强度留在「自动」时，引擎在**每次调用模型前**根据当前上下文估一个
档位，随任务推进逐轮升降：寒暄、翻译、格式整理走 low（更快更省），多步工具
作业维持 medium，调试排错、方案设计类任务升到 high。纯启发式、零额外请求；
估出的档位经 stream(effort=...) 逐调用下发，只对声明支持思考强度的服务实际生效
（openai_compat 传 reasoning_effort，anthropic 映射 thinking.budget_tokens）。
"""

from __future__ import annotations

import re

from ..messages import Message, ToolResultBlock, ToolUseBlock

# 寒暄/应答类：整条消息就是这些词 → 直接 low
_TRIVIAL_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|hey|在吗|在么|谢谢|多谢|感谢|辛苦了|麻烦了|"
    r"再见|拜拜|晚安|早安|好的|收到|明白|了解|ok|okay)[!！。~,，.~～\s]*$",
    re.IGNORECASE,
)

# 轻量任务：信息搬运为主，不太需要推理
_SIMPLE_TASK_RE = re.compile(
    r"翻译|润色|校对|错别字|错字|格式化|排版|总结|摘要|概括|改写|续写|缩写"
)

# 复杂任务信号：调试排错 / 方案设计 / 深度推演（命中多处再额外加权）
_COMPLEX_RE = re.compile(
    r"调试|排错|排查|定位问题|报错|异常|崩溃|卡死|修一下|修复|bug|error|exception|"
    r"traceback|stack\s?trace|架构|设计方案|方案设计|重构|迁移|性能|优化|瓶颈|并发|"
    r"安全|漏洞|注入|加密|原理|底层|源码|评审|review|选型|对比|权衡|从零|完整实现|"
    r"多步骤|分阶段|系统集成|数据流|状态机|算法|递归|边界条件"
)

# 代码特征：围栏代码块或常见代码片段
_CODE_RE = re.compile(
    r"```|def\s+\w+|class\s+\w+|function\s+\w+|import\s+\w+|const\s+\w+|let\s+\w+|"
    r"#include|void\s+\w+\(|SELECT\s+\S+\s+FROM|pip\s+install|npm\s+(i|install|run)"
)

# 参与信号统计的近期历史条数
_TAIL = 12
# 近期工具调用次数达到该值视为多步作业进行中
_MANY_TOOL_CALLS = 4


def estimate_effort(messages: list[Message]) -> str:
    """按当前上下文估一档思考强度，返回 low / medium / high（绝不返回 auto）。"""
    score = 1  # 基准：medium
    non_system = [m for m in messages if m.role != "system"]
    if not non_system:
        return "medium"

    # ---- 最新一条用户消息：任务本身的复杂度 ----
    user_texts = [
        "".join(b.text for b in m.content if getattr(b, "type", "") == "text")
        for m in non_system if m.role == "user"
    ]
    latest = (user_texts[-1] if user_texts else "").strip()
    if latest:
        if _TRIVIAL_RE.match(latest):
            score -= 2
        if _SIMPLE_TASK_RE.search(latest):
            score -= 1
        n_complex = len(set(_COMPLEX_RE.findall(latest)))
        if n_complex:
            score += 1 if n_complex == 1 else 2
        if _CODE_RE.search(latest):
            score += 1
        if len(latest) >= 400:
            score += 1

    # ---- 近期历史：任务推进中的动态信号（实时升降的关键） ----
    for m in non_system[-_TAIL:]:
        for b in m.content:
            if isinstance(b, ToolResultBlock) and b.is_error:
                score += 1  # 工具在报错：多半在排查，往深里调
                break
    tool_calls = sum(
        1 for m in non_system[-_TAIL:] for b in m.content if isinstance(b, ToolUseBlock)
    )
    if tool_calls >= _MANY_TOOL_CALLS:
        score += 1  # 多步作业进行中：保证足够的推理深度

    if score <= 0:
        return "low"
    if score >= 3:
        return "high"
    return "medium"


def resolve_auto_effort(provider, messages: list[Message]) -> str | None:
    """「自动」档的逐调用解析：auto 且声明支持思考强度 → 估档；否则 None。

    返回 None 表示不覆盖，stream 沿用 provider 自身档位（auto 时即不发参数，
    保持服务默认——未声明支持的服务永远走这条路）。
    """
    if getattr(provider, "reasoning_effort", "auto") != "auto":
        return None
    if not getattr(provider, "supports_reasoning", False):
        return None
    return estimate_effort(messages)
