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


def system_text(messages: list[Message]) -> str:
    """取出 system 提示词（各 Provider 单独处理，不进入对话体）。"""
    return "\n\n".join(m.text for m in messages if m.role == "system")


def dialogue(messages: list[Message]) -> list[Message]:
    """去掉 system 消息后的对话体。"""
    return [m for m in messages if m.role != "system"]
