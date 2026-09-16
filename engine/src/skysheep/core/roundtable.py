"""圆桌：多模型并行独立作答 + 主席融合。

一次圆桌轮的流程（事件流驱动，与 Agent.run_turn 消费同一套事件机制）：

    RoundtableStarted
      → N 个成员并行独立作答（无工具，纯思考；逐成员流式 RoundtableMemberDelta）
      → 各成员 RoundtableMemberFinished（done | error，单成员失败不阻断整场）
      → 主席融合：读各成员草稿 → 一份更好的最终答案（正常 TextDelta 流式）
      → 融合文本由调用方并入主历史

设计约定：
- 全程不传工具表（tool_schemas=[]），不经过 PermissionGate，无新增安全面；
- 成员用独立 Provider 实例，主席复用当前主模型；成员作答与融合串行衔接，
  不存在同一 Provider 实例的并发调用；
- 主历史只保留「user(问题) + assistant(融合答案)」，草稿通过事件流与
  消息元数据留存，不进入主历史；
- 瞬态错误复用 agent.py 的判定与重试节奏；只在还没吐出任何内容时重试
  （否则重放会导致卡片文本重复）；成员卡死由 member_timeout_s 截断。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..events import (
    AgentEvent,
    NoticeEvent,
    RoundtableMemberDelta,
    RoundtableMemberFinished,
    RoundtableStarted,
    TextDelta,
    Usage,
)
from ..messages import Message, dialogue
from ..models.base import Provider, ProviderDone, ProviderTextDelta
from .agent import MAX_STREAM_RETRIES, RETRY_BASE_DELAY_S, is_transient_error

MEMBER_SYSTEM = (
    "你是圆桌讨论的成员之一。请针对用户的问题独立给出你的最佳回答："
    "直接给结论与理由，结构清晰、内容完整。"
    "不要询问其他人的意见，不要反问用户，不要输出与问题无关的客套话。"
)

FUSION_INSTRUCTION = (
    "上面给出了多个 AI 模型对同一问题的独立回答草稿。"
    "请把它们融合成一份更好的最终回答：\n"
    "1. 采纳各草稿中正确、互补的内容，修正其中的错误与遗漏；\n"
    "2. 草稿之间有分歧时，给出你的判断与理由，不要机械罗列；\n"
    "3. 输出一份连贯、自洽、面向用户的最终回答，"
    "不要提及「成员」「草稿」「模型A/B」等融合过程。"
)

# 发给前端的事件回调（backend 负责把它转成 WS 帧广播）
EmitFn = Callable[[AgentEvent], Awaitable[None]]


@dataclass
class MemberSpec:
    """一个圆桌成员：provider/model 标识 + 已构建的 Provider 实例。"""

    provider_name: str
    model: str
    provider: Provider | None = None  # 构建失败（如缺 Key）时为 None，作答时直接报错
    build_error: str = ""


@dataclass
class MemberResult:
    spec: MemberSpec
    index: int
    text: str = ""
    status: str = "done"  # done | error
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def label(self) -> str:
        return f"{self.spec.provider_name}/{self.spec.model}"


@dataclass
class RoundtableOutcome:
    """整场圆桌的结果：成员草稿 + 融合文本与用量。"""

    members: list[MemberResult] = field(default_factory=list)
    fused_text: str = ""
    status: str = "done"  # done | error
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _label(spec: MemberSpec) -> str:
    return f"{spec.provider_name}/{spec.model}"


async def _run_member(
    spec: MemberSpec,
    index: int,
    history: list[Message],
    user_text: str,
    timeout_s: int,
    emit: EmitFn,
) -> MemberResult:
    """一个成员的独立作答：重试/超时/容错都收敛在自己身上，失败不影响其他成员。

    history 不含本次提问（由本函数拼在最后），保证成员消息序列规整。
    """
    result = MemberResult(spec=spec, index=index)

    async def finish(status: str, error: str = "") -> MemberResult:
        result.status = status
        result.error = error
        await emit(RoundtableMemberFinished(
            member_index=index, status=status, error=error,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        ))
        if result.input_tokens or result.output_tokens:
            await emit(Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens))
        return result

    if spec.provider is None:
        return await finish("error", spec.build_error or "模型服务不可用")

    messages = [Message.system(MEMBER_SYSTEM), *dialogue(history), Message.user(user_text)]
    parts: list[str] = []

    async def consume() -> None:
        async for pe in spec.provider.stream(messages, []):
            if isinstance(pe, ProviderTextDelta):
                parts.append(pe.text)
                await emit(RoundtableMemberDelta(member_index=index, text=pe.text))
            elif isinstance(pe, ProviderDone):
                result.input_tokens += pe.input_tokens
                result.output_tokens += pe.output_tokens

    for attempt in range(1, MAX_STREAM_RETRIES + 2):
        parts = []
        result.input_tokens = 0
        result.output_tokens = 0
        error = ""
        retryable = False
        try:
            await asyncio.wait_for(consume(), timeout=timeout_s)
            result.text = "".join(parts)
            return await finish("done")
        except TimeoutError:
            error = f"作答超时（>{timeout_s} 秒）"
            retryable = not parts and attempt <= MAX_STREAM_RETRIES
        except Exception as e:  # noqa: BLE001 - 任何异常都归入该成员的失败，不上抛
            error = str(e)[:200]
            retryable = (
                not parts and attempt <= MAX_STREAM_RETRIES and is_transient_error(e)
            )
        if retryable:
            await _notice_retry(emit, _label(spec), error, attempt)
            continue
        # 超时 / 非瞬态错误 / 已吐出内容：如实失败，保留已有草稿片段
        if parts:
            result.text = "".join(parts)
        return await finish("error", error)

    return await finish("error", "重试次数用尽")  # pragma: no cover - 防御性兜底


async def _notice_retry(emit: EmitFn, label: str, error: str, attempt: int) -> None:
    delay = min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), 10.0)
    await emit(NoticeEvent(
        message=f"圆桌成员 {label} 调用失败（{error[:120]}），"
                f"{delay:.0f} 秒后自动重试（第 {attempt}/{MAX_STREAM_RETRIES} 次）…"
    ))
    await asyncio.sleep(delay)


def drafts_section(results: list[MemberResult]) -> str:
    """把成员草稿排版成融合提示词里的分节文本。"""
    sections: list[str] = []
    for r in results:
        if r.status == "done" and r.text.strip():
            body = r.text.strip()
        else:
            body = f"（该成员作答失败：{r.error or '未知错误'}）"
        sections.append(f"### 成员{r.index + 1}（{r.label}）\n{body}")
    return "\n\n".join(sections)


def build_fusion_messages(
    system_text: str,
    history: list[Message],
    question: str,
    results: list[MemberResult],
) -> list[Message]:
    """融合轮消息：主历史（不含本次提问）+ 合并了问题/指令/草稿的最终 user 消息。

    把问题并入融合指令（而不是在历史末尾追加第二条 user 消息），
    保证 openai/anthropic 两种协议都不会出现连续同角色消息。
    """
    msgs: list[Message] = []
    if system_text:
        msgs.append(Message.system(system_text))
    msgs.extend(dialogue(history))
    combined = (
        f"{question}\n\n---\n\n{FUSION_INSTRUCTION}\n\n"
        f"## 各成员独立草稿\n\n{drafts_section(results)}"
    )
    msgs.append(Message.user(combined))
    return msgs


async def _run_fusion(
    chair: Provider,
    messages: list[Message],
    timeout_s: int,
    emit: EmitFn,
) -> tuple[str, str, int, int]:
    """主席融合：流式产出最终答案。返回 (text, error, input_tokens, output_tokens)。

    失败但已有部分文本时，保留部分文本一并返回（用户已经看到了，落库保持一致）。
    """
    parts: list[str] = []
    usage_in = usage_out = 0

    async def consume() -> None:
        nonlocal usage_in, usage_out
        async for pe in chair.stream(messages, []):
            if isinstance(pe, ProviderTextDelta):
                parts.append(pe.text)
                await emit(TextDelta(text=pe.text))
            elif isinstance(pe, ProviderDone):
                usage_in += pe.input_tokens
                usage_out += pe.output_tokens

    error = ""
    for attempt in range(1, MAX_STREAM_RETRIES + 2):
        parts = []
        usage_in = usage_out = 0
        retryable = False
        try:
            await asyncio.wait_for(consume(), timeout=timeout_s)
            return "".join(parts), "", usage_in, usage_out
        except TimeoutError:
            error = f"融合超时（>{timeout_s} 秒）"
            retryable = not parts and attempt <= MAX_STREAM_RETRIES
        except Exception as e:  # noqa: BLE001
            error = str(e)[:200]
            retryable = (
                not parts and attempt <= MAX_STREAM_RETRIES and is_transient_error(e)
            )
        if retryable:
            await _notice_retry(emit, "融合", error, attempt)
            continue
        break  # 超时 / 非瞬态错误 / 已有内容：停止重试

    return "".join(parts), error, usage_in, usage_out


async def run_roundtable(
    *,
    members: list[MemberSpec],
    chair: Provider,
    system_text: str,
    history: list[Message],
    user_text: str,
    timeout_s: int,
    emit: EmitFn,
) -> RoundtableOutcome:
    """跑一整场圆桌：并行作答 → 主席融合。返回完整结果，不直接改写主历史。"""
    await emit(RoundtableStarted(members=[
        {"index": i, "provider": s.provider_name, "model": s.model}
        for i, s in enumerate(members)
    ]))

    results = list(await asyncio.gather(*(
        _run_member(spec, i, history, user_text, timeout_s, emit)
        for i, spec in enumerate(members)
    )))

    outcome = RoundtableOutcome(members=results)
    fusion_messages = build_fusion_messages(system_text, history, user_text, results)
    fused, error, usage_in, usage_out = await _run_fusion(
        chair, fusion_messages, max(timeout_s * 2, 300), emit
    )
    outcome.fused_text = fused
    outcome.input_tokens = sum(r.input_tokens for r in results) + usage_in
    outcome.output_tokens = sum(r.output_tokens for r in results) + usage_out
    if usage_in or usage_out:
        await emit(Usage(input_tokens=usage_in, output_tokens=usage_out))

    if error:
        outcome.status = "error"
        outcome.error = error
    return outcome
