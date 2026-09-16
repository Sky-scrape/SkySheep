"""上下文管理：token 估算与历史压缩（compaction）。

长会话的历史会撑爆模型上下文窗口。策略：
- 估算 token（CJK 与西文分开折算，见下；真实 usage 用来兜底下限）；
- 超过 context_limit_tokens 时，保留 system + 最近 keep_recent 条消息，
  其余历史用一次专用 LLM 调用生成结构化摘要，替换为单条 user 消息；
- 切分边界保证「保留段」不以孤儿 tool_result 开头（协议一致性要求）。
"""

from __future__ import annotations

import re

from ..events import CompactionEvent
from ..messages import Message

# 西文/代码约 3.5 字符一个 token；CJK 一个字符约 0.7 个 token。
# 早期版本一律按 3.5 折算，中文会话的占用率会被低估约 2 倍（中文并不是
# 3.5 个字符才一个 token），环形仪表显得很乐观、自动压缩触发过晚，
# 长中文会话有撞上上游上下文上限直接报错的风险。
OTHER_CHARS_PER_TOKEN = 3.5
CJK_TOKENS_PER_CHAR = 0.7
# CJK 标点 / 假名 / 汉字（含扩展 A、兼容区、扩展 B 以上）/ 全角符号
_CJK_RE = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    "\uf900-\ufaff\uff00-\uffef\U00020000-\U0003ffff]"
)

COMPACT_SYSTEM = (
    "You are a conversation summarizer for a coding agent. "
    "Produce a dense, factual summary in the SAME LANGUAGE as the conversation."
)

COMPACT_USER_TEMPLATE = """\
Summarize the conversation segment below for an AI agent that will continue the task \
with no other memory. Capture, in compact markdown:
1. The user's goal(s) and any constraints
2. What has been done so far (files created/modified, commands run, key results)
3. Important file paths, errors encountered and their causes, decisions made
4. What remains to be done

--- CONVERSATION SEGMENT ---
{transcript}
--- END ---"""


def estimate_text_tokens(text: str) -> int:
    """按字符类别估算 token 数（CJK 与西文分别折算）。"""
    cjk = sum(1 for _ in _CJK_RE.finditer(text))
    other = len(text) - cjk
    return int(cjk * CJK_TOKENS_PER_CHAR + other / OTHER_CHARS_PER_TOKEN)


def estimate_tokens(messages: list[Message]) -> int:
    return estimate_text_tokens("".join(m.to_plain() for m in messages))


def _safe_recent_start(messages: list[Message], keep: int) -> int:
    """返回保留段的起始下标：保证保留段首条不是孤儿 tool_result
    或"带 tool_use 的 assistant"（避免上游工具调用被摘要掉导致协议断裂）。"""
    start = max(1, len(messages) - keep)  # 跳过 system（下标 0）
    while start < len(messages):
        m = messages[start]
        if m.role == "tool":
            start -= 1
            continue
        if m.role == "assistant" and m.tool_uses:
            # 该 assistant 的 tool_use 需要与其 tool_result 在同一段；
            # 前移一位通常能把它们一起划入保留段或一起划出
            start -= 1
            continue
        break
    return max(1, start)


async def compact_history(
    agent,  # Agent，避免循环导入用鸭子类型
    keep_recent: int = 8,
) -> CompactionEvent | None:
    """压缩 agent.history；没有可压缩内容时返回 None。"""
    history = agent.history
    if len(history) <= keep_recent + 1:
        return None

    start = _safe_recent_start(history, keep_recent)
    to_summarize = history[1:start]
    if not to_summarize:
        return None
    recent = history[start:]

    transcript = "\n\n".join(f"[{m.role.upper()}] {m.to_plain()}" for m in to_summarize)
    summary_parts: list[str] = []
    async for pe in agent.provider.stream(
        [Message.system(COMPACT_SYSTEM), Message.user(COMPACT_USER_TEMPLATE.format(transcript=transcript))],
        [],
    ):
        if hasattr(pe, "text"):
            summary_parts.append(pe.text)
    summary = "".join(summary_parts).strip()
    if not summary:
        return None

    summary_msg = Message.user(
        f"<earlier-conversation-summary>\n{summary}\n</earlier-conversation-summary>\n"
        "(以上是此前对话的摘要，原始消息已省略。)"
    )
    agent.history = [history[0], summary_msg] + list(recent)
    return CompactionEvent(
        before_messages=len(history),
        after_messages=len(agent.history),
        summary_chars=len(summary),
    )
