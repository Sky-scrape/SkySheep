"""归一化的消息与内容块模型。

所有 Provider（OpenAI 兼容 / Anthropic）的对话历史都转换为本模型存储，
由各 Provider 负责序列化回自己的线上格式。
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    type: Literal["thinking"] = "thinking"
    text: str
    # Anthropic 扩展思考的签名（回传历史时 API 要求携带）；其他服务为空。
    # 没有签名的思考块回传时会被 anthropic_provider 静默跳过。
    signature: str = ""
    # 这段推理实际花掉的毫秒数（首个 thinking 增量到最后一个增量的跨度）。
    # 只用于展示「思考用了多久」；不入线上 payload，也不参与 FTS 索引。
    duration_ms: int = 0


class ToolUseBlock(BaseModel):
    """助手发起的一次工具调用。"""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """工具执行结果，role=tool 消息中携带。"""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str = ""
    is_error: bool = False


class ImageBlock(BaseModel):
    """用户消息携带的图片附件（base64，不带 data: 前缀）。

    只在 role=user 的消息中出现；openai_compat / anthropic 序列化时各自
    转成线上格式（image_url data URL / image source block）。
    """

    type: Literal["image"] = "image"
    media_type: str = "image/png"  # image/png | image/jpeg | image/webp | image/gif
    data: str = ""


ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | ImageBlock

Role = Literal["system", "user", "assistant", "tool"]


def _short_id() -> str:
    return uuid.uuid4().hex[:16]


class Message(BaseModel):
    role: Role
    content: list[ContentBlock] = Field(default_factory=list)
    id: str = Field(default_factory=_short_id)
    created_at: float = Field(default_factory=time.time)
    # 圆桌轮元数据（仅 assistant 消息携带）：成员列表、主席、模式等。
    # 只随消息持久化/回传前端，Provider 序列化线上 payload 时不涉及此字段。
    roundtable: dict | None = None
    # 消息在会话内的序号（store 落库时回填；前端消息级操作用）。不入线上 payload。
    seq: int | None = None
    # 本轮的实测耗时（毫秒，仅轮末 assistant 消息携带）。
    #
    # 为什么落在消息上而不是新建数据库列：一轮的 user/assistant 消息是轮末
    # 批量落库的，两条 created_at 只差几毫秒，按时间差反推轮耗时从来算不准
    #（core/estimate.py 的历史校准因此一直拿不到样本）。把真实耗时随消息内容
    # 一起存，刷新/重进会话后仍能显示「用时 X」，也不需要对 messages 表做
    # ALTER TABLE 迁移。只持久化/回传前端，不进 Provider 线上 payload。
    duration_ms: int = 0
    # 本轮接手时下发的预估区间（秒），供历史恢复后重建「用时 X · 预估 Y」芯片。
    # 与 duration_ms 同批写入；旧消息没有这两个字段时前端不渲染芯片。
    estimate: dict | None = None

    # ---- 便捷构造 ----

    @classmethod
    def system(cls, text: str) -> Message:
        return cls(role="system", content=[TextBlock(text=text)])

    @classmethod
    def user(cls, text: str, images: list[ImageBlock] | None = None) -> Message:
        content: list[ContentBlock] = []
        if text:
            content.append(TextBlock(text=text))
        content.extend(images or [])
        if not content:
            content = [TextBlock(text="")]
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, blocks: list[ContentBlock]) -> Message:
        return cls(role="assistant", content=blocks)

    @classmethod
    def tool_result(cls, tool_use_id: str, content: str, *, is_error: bool = False) -> Message:
        return cls(
            role="tool",
            content=[ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=is_error)],
        )

    # ---- 便捷读取 ----

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def to_plain(self) -> str:
        """调试/摘要用的纯文本表示。"""
        parts: list[str] = []
        for b in self.content:
            if isinstance(b, TextBlock):
                parts.append(b.text)
            elif isinstance(b, ThinkingBlock):
                parts.append(f"[thinking] {b.text}")
            elif isinstance(b, ToolUseBlock):
                parts.append(f"[tool_use {b.name}] {b.input}")
            elif isinstance(b, ToolResultBlock):
                flag = " ERROR" if b.is_error else ""
                parts.append(f"[tool_result{flag}] {b.content}")
            elif isinstance(b, ImageBlock):
                parts.append(f"[image {b.media_type} {len(b.data)}b(base64)]")
        return "\n".join(parts)

    @property
    def thinking(self) -> str:
        """推理文本（无思考块时为空串）。"""
        return "".join(b.text for b in self.content if isinstance(b, ThinkingBlock))

    @property
    def thinking_ms(self) -> int:
        """本消息思考块实测耗时（毫秒，无思考块为 0）。"""
        return max(
            (getattr(b, "duration_ms", 0) for b in self.content
             if isinstance(b, ThinkingBlock)),
            default=0,
        )


def system_text(messages: list[Message]) -> str:
    """取出 system 提示词（各 Provider 单独处理，不进入对话体）。"""
    return "\n\n".join(m.text for m in messages if m.role == "system")


def dialogue(messages: list[Message]) -> list[Message]:
    """去掉 system 消息后的对话体。"""
    return [m for m in messages if m.role != "system"]
