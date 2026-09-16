"""FakeProvider：脚本化回放的假模型。

用途：无 API Key 时的端到端测试、UI 开发、演示。
每次 stream() 依次弹出脚本中的一组内容块（TextBlock / ThinkingBlock / ToolUseBlock），
脚本耗尽后重复 scripted_default。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..messages import Message, TextBlock, ThinkingBlock, ToolUseBlock
from .base import Provider, ProviderDone, ProviderReasoning, ProviderTextDelta, ProviderToolUse


class FakeProvider(Provider):
    name = "fake"
    model = "fake-1"

    def __init__(self, scripted: list[list]) -> None:
        super().__init__()
        self.scripted = list(scripted)
        self.scripted_default: list = []
        self.calls: list[list[Message]] = []

    def with_default(self, blocks: list) -> FakeProvider:
        self.scripted_default = blocks
        return self

    async def stream(self, messages: list[Message], tool_schemas: list[dict]) -> AsyncIterator:
        self.calls.append(list(messages))
        blocks = self.scripted.pop(0) if self.scripted else self.scripted_default
        tool_used = False
        for b in blocks:
            if isinstance(b, TextBlock):
                for i in range(0, len(b.text), 4):
                    yield ProviderTextDelta(b.text[i : i + 4])
            elif isinstance(b, ThinkingBlock):
                # 思考内容按增量产出，与真实思考型模型的流式行为一致
                for i in range(0, len(b.text), 4):
                    yield ProviderReasoning(b.text[i : i + 4])
            else:
                tool_used = True
                yield ProviderToolUse(id=b.id, name=b.name, input=b.input)
        yield ProviderDone(
            stop_reason="tool_use" if tool_used else "end_turn",
            input_tokens=11,
            output_tokens=7,
        )


# ---- 首启向导「演示模式」脚本 ----
# 设计原则：只用只读工具（list_dir 自动放行，不弹确认），保证演示零摩擦、
# 永不失败；确认制等安全机制靠文案讲解而不是现场触发。脚本共两组：
# 第一组带一次 list_dir 工具调用，第二组是收尾总结；之后的任何额外调用
# （如标题生成）落到 scripted_default，不影响会话。
DEMO_SCRIPT: list[list] = [
    [
        TextBlock(text=(
            "你好，我是 SkySheep 的演示模式 🐑\n\n"
            "这个模式不需要任何 API Key，用一段内置脚本模拟模型回复，"
            "让你先看看 Agent 的工作方式。我现在做一件真实的事："
            "看看你的工作目录里有什么。\n"
        )),
        ToolUseBlock(id="demo-1", name="list_dir", input={"path": "."}),
    ],
    [
        TextBlock(text=(
            "看到了，上面就是你的工作目录——上面那次目录列表是**真实执行**的，"
            "只有「说话」的部分是脚本。\n\n"
            "正式使用时，模型会这样一步步干活：读取文件 → 执行命令 → 写出结果，"
            "而所有写文件、执行命令都会先弹出确认卡片征求你的同意，"
            "每一轮改动都能一键撤销。\n\n"
            "**接下来**：点 ⚙ 设置 → 模型服务，填入任意一家的 API Key"
            "（智谱 / DeepSeek / Kimi 都有免费或低价额度），"
            "或者装了 [Ollama](https://ollama.com) 的话可以直接用本地模型，无需 Key。\n\n"
            "_演示模式结束，这条之后不再有新内容。_"
        )),
    ],
]
DEMO_DEFAULT_REPLY = [
    TextBlock(text="（演示模式运行中——配置一个真实模型服务后即可开始正式使用。）"),
]
