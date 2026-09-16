"""OpenAI 兼容协议 Provider（官方 openai SDK）。

覆盖 DeepSeek / GLM / Kimi / OpenRouter / SiliconFlow / Ollama 等
所有兼容 chat.completions 协议的服务。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from openai import AsyncOpenAI

from ..messages import ImageBlock, Message, ThinkingBlock, ToolResultBlock, dialogue, system_text
from .base import (
    Provider,
    ProviderDone,
    ProviderError,
    ProviderEvent,
    ProviderReasoning,
    ProviderTextDelta,
    ProviderToolUse,
)


def _thinking_text(m: Message) -> str:
    return "".join(b.text for b in m.content if isinstance(b, ThinkingBlock))


def to_openai_messages(messages: list[Message]) -> list[dict]:
    sys = system_text(messages)
    out: list[dict] = []
    if sys:
        out.append({"role": "system", "content": sys})
    for m in dialogue(messages):
        if m.role == "user":
            imgs = [b for b in m.content if isinstance(b, ImageBlock)]
            if imgs:
                # 多模态：文本 + 图片按 OpenAI content parts 格式拼接
                parts: list[dict] = [
                    {"type": "text", "text": m.text}
                ] if m.text else []
                parts.extend(
                    {"type": "image_url",
                     "image_url": {"url": f"data:{b.media_type};base64,{b.data}"}}
                    for b in imgs
                )
                out.append({"role": "user", "content": parts})
            else:
                out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            payload: dict = {"role": "assistant", "content": m.text or None}
            # 思考型模型（DeepSeek reasoning_content 等）要求把上一轮推理内容回传
            reasoning = _thinking_text(m)
            if reasoning:
                payload["reasoning_content"] = reasoning
            calls = []
            for tu in m.tool_uses:
                calls.append(
                    {
                        "id": tu.id,
                        "type": "function",
                        "function": {"name": tu.name, "arguments": json.dumps(tu.input, ensure_ascii=False)},
                    }
                )
            if calls:
                payload["tool_calls"] = calls
            out.append(payload)
        elif m.role == "tool":
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b.tool_use_id,
                            "content": b.content,
                        }
                    )
    return out


class OpenAICompatProvider(Provider):
    def __init__(
        self,
        name: str,
        model: str,
        api_key: str,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
        proxy: str = "",
    ) -> None:
        super().__init__()
        self.name = name
        self.model = model
        # 代理：服务级设置（如 http://127.0.0.1:7890）；格式错误由 httpx 报可读错误
        kw: dict = {"api_key": api_key, "base_url": base_url, "timeout": 300.0, "max_retries": 2}
        if proxy:
            kw["http_client"] = httpx.AsyncClient(proxy=proxy, timeout=300.0, trust_env=False)
        self._client = client or AsyncOpenAI(**kw)

    async def stream(
        self, messages: list[Message], tool_schemas: list[dict]
    ) -> AsyncIterator[ProviderEvent]:
        params: dict = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "stream": True,
        }
        # 思考强度：auto 不传（沿用服务默认），其余档位透传（OpenAI/兼容网关通用参数）
        if self.supports_reasoning and self.reasoning_effort != "auto":
            params["reasoning_effort"] = self.reasoning_effort
        # 采样温度：只有用户在服务设置里填了才传，否则完全交给服务默认
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if tool_schemas:
            params["tools"] = [
                {"type": "function", "function": s} for s in tool_schemas
            ]
        try:
            stream = await self._client.chat.completions.create(**params)
            tool_acc: dict[int, dict] = {}
            in_tokens = out_tokens = 0
            stop = "end_turn"
            async for chunk in stream:
                if chunk.usage:
                    in_tokens = chunk.usage.prompt_tokens or in_tokens
                    out_tokens = chunk.usage.completion_tokens or out_tokens
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                rc = getattr(delta, "reasoning_content", None) if delta else None
                if rc:
                    # 思考内容逐段透出（DeepSeek-R1 / GLM 思考模型），前端实时可折叠展示
                    yield ProviderReasoning(rc)
                if delta and delta.content:
                    yield ProviderTextDelta(delta.content)
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        acc = tool_acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function and tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            acc["args"] += tc.function.arguments
                if choice.finish_reason:
                    stop = "tool_use" if choice.finish_reason == "tool_calls" else "end_turn"
            for idx in sorted(tool_acc):
                acc = tool_acc[idx]
                try:
                    data = json.loads(acc["args"] or "{}")
                except json.JSONDecodeError:
                    data = {"_raw_arguments": acc["args"]}
                yield ProviderToolUse(
                    id=acc["id"] or f"call_{idx}", name=acc["name"], input=data
                )
            yield ProviderDone(
                stop_reason=stop if tool_acc or stop == "end_turn" else "end_turn",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"[{self.name}] {e}") from e
