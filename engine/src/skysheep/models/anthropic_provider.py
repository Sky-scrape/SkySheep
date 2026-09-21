"""Anthropic 原生协议 Provider（官方 anthropic SDK）。

支持流式输出与 prompt caching（system 与最后一条消息各打一个 ephemeral
缓存点，对话历史在迭代间增量命中缓存）。
连续的 tool_result 消息会合并进单条 user 消息（Anthropic 协议要求）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ..messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    dialogue,
    system_text,
)

if TYPE_CHECKING:
    # 仅注解用：运行时延迟到建客户端时才导入（anthropic 包导入约 0.75s，启动期用不到）
    from anthropic import AsyncAnthropic

from .base import (
    Provider,
    ProviderDone,
    ProviderError,
    ProviderEvent,
    ProviderReasoning,
    ProviderTextDelta,
    ProviderToolUse,
)


def _mark_cache_breakpoint(msgs: list[dict]) -> None:
    """在最后一条消息的末尾内容块上打缓存断点（原地修改，增量缓存对话历史）。

    system 上已有一个断点（只覆盖工具定义+系统提示词）；对话历史每轮迭代
    只追加不改动，断点打在最后一条消息上即可让上一轮请求的全部前缀命中
    缓存（Anthropic 允许至多 4 个断点，这里固定用 2 个）。长会话的历史
    主体是省钱大头，没有这个断点就永远按全价 input 计费。
    thinking 块不接受 cache_control，遇到时落到它前面最近的块上。
    """
    if not msgs:
        return
    content = msgs[-1]["content"]
    if isinstance(content, str):
        msgs[-1]["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return
    for block in reversed(content):
        if block.get("type") != "thinking":
            block["cache_control"] = {"type": "ephemeral"}
            return


def to_anthropic_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in dialogue(messages):
        if m.role == "user":
            imgs = [b for b in m.content if isinstance(b, ImageBlock)]
            if imgs:
                content: list[dict] = [{"type": "text", "text": m.text}] if m.text else []
                content.extend(
                    {"type": "image",
                     "source": {"type": "base64", "media_type": b.media_type, "data": b.data}}
                    for b in imgs
                )
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": m.text})
            continue
        if m.role == "assistant":
            blocks: list[dict] = []
            for b in m.content:
                if isinstance(b, TextBlock):
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ThinkingBlock) and b.text and b.signature:
                    # 扩展思考的回传需要签名；无签名的思考块（其他服务或旧记录）跳过
                    blocks.append({"type": "thinking", "thinking": b.text, "signature": b.signature})
                elif isinstance(b, ToolUseBlock):
                    blocks.append(
                        {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                    )
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
        # role == "tool"：合并连续 tool_result 到一条 user 消息
        if m.role == "tool":
            results: list[dict] = []
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    item = {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": b.content}
                    if b.is_error:
                        item["is_error"] = True
                    results.append(item)
            if not results:
                continue
            if out and out[-1]["role"] == "user" and all(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in out[-1]["content"]
            ):
                out[-1]["content"].extend(results)
            else:
                out.append({"role": "user", "content": results})
    return out


class AnthropicProvider(Provider):
    # 思考强度档位 → thinking.budget_tokens
    THINKING_BUDGETS = {"low": 4096, "medium": 10240, "high": 24576}

    def __init__(
        self,
        name: str,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 8192,
        client: AsyncAnthropic | None = None,
        proxy: str = "",
    ) -> None:
        super().__init__()
        self.name = name
        self.model = model
        self._max_tokens = max_tokens
        # SDK 内置重试降为 1：agent/圆桌层有自己的重试编排（带退避与「已吐内容
        # 不重放」判断，还会发通知事件），SDK 再各自叠 2 次会把持续性限流的
        # 最坏等待翻倍，界面长时间卡在「第 1/3 次重试」上
        kw: dict = {"api_key": api_key, "base_url": base_url, "timeout": 300.0, "max_retries": 1}
        if proxy:
            # anthropic SDK 同样接受 http_client；不同 SDK 版本绑定 httpx 或 httpx2
            try:
                import httpx2 as _httpx
            except ImportError:
                import httpx as _httpx
            kw["http_client"] = _httpx.AsyncClient(proxy=proxy, timeout=300.0, trust_env=False)
        from anthropic import AsyncAnthropic  # noqa: PLC0415  启动性能：见 TYPE_CHECKING 处注释

        self._client = client or AsyncAnthropic(**kw)

    async def stream(
        self, messages: list[Message], tool_schemas: list[dict],
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        sys = system_text(messages)
        msgs = to_anthropic_messages(messages)
        _mark_cache_breakpoint(msgs)
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": msgs,
        }
        if sys:
            kwargs["system"] = [{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}]
        if tool_schemas:
            kwargs["tools"] = [
                {"name": s["name"], "description": s["description"], "input_schema": s["input_schema"]}
                for s in tool_schemas
            ]
        # 思考强度 → 扩展思考预算（auto 不启用，保持服务默认行为）；
        # effort 覆盖（自动档实时估档，见 core/effort.py）优先于自身档位
        eff = effort or self.reasoning_effort
        budget = self.THINKING_BUDGETS.get(eff) if self.supports_reasoning else None
        if budget:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # 采样温度：只有用户填了才传（扩展思考模式下温度固定为 1，传了反而会被拒绝）
        if self.temperature is not None and not budget:
            kwargs["temperature"] = self.temperature
        try:
            in_tokens = out_tokens = cached_tokens = 0
            tool_blocks: dict[int, dict] = {}
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    etype = event.type
                    if etype == "content_block_start" and event.content_block.type == "tool_use":
                        tool_blocks[event.index] = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "json": "",
                        }
                    elif etype == "content_block_delta":
                        d = event.delta
                        if d.type == "text_delta":
                            yield ProviderTextDelta(d.text)
                        elif d.type == "thinking_delta":
                            # 扩展思考：思考文本增量实时透出（前端可折叠展示）
                            yield ProviderReasoning(d.thinking)
                        elif d.type == "signature_delta":
                            # 签名单独送达（text 为空的增量只携带签名）
                            yield ProviderReasoning("", signature=d.signature)
                        elif d.type == "input_json_delta" and event.index in tool_blocks:
                            tool_blocks[event.index]["json"] += d.partial_json
                    elif etype == "message_delta":
                        # stop_reason 不直接使用：是否调用工具由 tool_blocks 判断
                        if event.usage:
                            out_tokens = event.usage.output_tokens or out_tokens
                    elif etype == "message_start" and event.message and event.message.usage:
                        u = event.message.usage
                        in_tokens = u.input_tokens or in_tokens
                        # Anthropic 的 input_tokens 不含缓存部分：归一成「完整提示词」
                        # （加上缓存读/写）；命中数只计 cache_read（本次真正省下的部分）
                        cr = getattr(u, "cache_read_input_tokens", 0) or 0
                        cc = getattr(u, "cache_creation_input_tokens", 0) or 0
                        if cr or cc:
                            in_tokens = (in_tokens or 0) + cr + cc
                            cached_tokens = cr
            for idx in sorted(tool_blocks):
                tb = tool_blocks[idx]
                try:
                    data = json.loads(tb["json"] or "{}")
                except json.JSONDecodeError:
                    data = {"_raw_arguments": tb["json"]}
                yield ProviderToolUse(id=tb["id"], name=tb["name"], input=data)
            yield ProviderDone(
                stop_reason="tool_use" if tool_blocks else "end_turn",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_tokens=cached_tokens,
            )
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"[{self.name}] {e}") from e
