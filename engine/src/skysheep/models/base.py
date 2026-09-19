"""Provider 抽象与流式事件。

Provider 把归一化的 Message 列表转换为自有协议格式，流式地产出：
- ProviderTextDelta：助手文本增量
- ProviderReasoning：思考型模型的推理内容（整块）
- ProviderToolUse：完整的工具调用块（流结束后产出）
- ProviderDone：本次响应结束（stop_reason: end_turn / tool_use）

Provider 内部异常统一包装为 ProviderError 抛出/产出。
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..messages import Message


@dataclass
class ProviderTextDelta:
    text: str


@dataclass
class ProviderReasoning:
    """思考型模型的推理内容增量（DeepSeek reasoning_content / Anthropic thinking）。

    流式期间逐段产出（与 ProviderTextDelta 对等）；Agent 循环累积为
    ThinkingBlock 存入助手消息，OpenAI 兼容协议回传时以 reasoning_content
    字段携带。text 为空的增量表示只携带签名（Anthropic 扩展思考）。
    """

    text: str
    signature: str = ""


@dataclass
class ProviderToolUse:
    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class ProviderDone:
    stop_reason: str = "end_turn"  # end_turn | tool_use
    input_tokens: int = 0
    output_tokens: int = 0
    # 本次请求命中提示词缓存的 token 数（input_tokens 已含这部分）。
    # 服务不上报缓存明细时为 0；用于界面「平均缓存命中率」。
    cached_tokens: int = 0


ProviderEvent = ProviderTextDelta | ProviderReasoning | ProviderToolUse | ProviderDone


class ProviderError(Exception):
    pass


class Provider(abc.ABC):
    name: str = ""
    model: str = ""
    # 是否支持思考强度；支持时 set_reasoning_effort 生效（各 Provider 自行映射）
    supports_reasoning: bool = False
    # 当前模型是否支持图片输入。False 时贴图/截图会先给出可读提示，
    # 而不是把图片塞给纯文本模型再收到一句上游报错。
    supports_vision: bool = True
    # 采样温度；None = 不发送该参数（沿用服务默认）
    temperature: float | None = None

    def __init__(self) -> None:
        self.reasoning_effort: str = "auto"

    def set_reasoning_effort(self, effort: str) -> str:
        """设置思考强度档位（auto/low/medium/high），返回实际生效值。"""
        effort = (str(effort or "auto")).strip().lower()
        if effort not in ("auto", "low", "medium", "high"):
            effort = "auto"
        self.reasoning_effort = effort
        return effort

    @abc.abstractmethod
    def stream(
        self, messages: list[Message], tool_schemas: list[dict],
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """发起一次流式对话。messages 含 system 时由 Provider 自行提取处理。

        effort：本次调用的思考强度覆盖（自动档按任务复杂度实时估档用，
        见 core/effort.py）；None = 沿用自身 reasoning_effort。
        """
