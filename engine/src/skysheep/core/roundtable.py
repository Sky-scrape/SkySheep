"""圆桌：多模型并行独立作答（可选多轮辩论）+ 主席融合。

一次圆桌轮的流程（事件流驱动，与 Agent.run_turn 消费同一套事件机制）：

    RoundtableStarted(rounds=总轮数)
      → 第 1 轮（round=0）：N 个成员并行独立作答（无工具，纯思考；
        逐成员流式 RoundtableMemberDelta）
      → 第 2..K 轮（可选辩论，round=1..K-1）：成员读到彼此上一轮草稿后
        修订重写；与上一轮草稿完全一致的成员自动跳过（已收敛，不再花钱）
      → 各成员 RoundtableMemberFinished（done | error，单成员失败不阻断整场）
      → 主席融合：读各成员最终草稿 → 一份更好的最终答案（正常 TextDelta 流式）
      → 融合文本由调用方并入主历史

设计约定：
- 全程不传工具表（tool_schemas=[]），不经过 PermissionGate，无新增安全面；
- 成员用独立 Provider 实例，主席复用当前主模型；成员作答与融合串行衔接，
  不存在同一 Provider 实例的并发调用；
- 主历史只保留「user(问题) + assistant(融合答案)」，草稿通过事件流与
  消息元数据留存，不进入主历史；
- 瞬态错误复用 agent.py 的判定与重试节奏；只在还没吐出任何内容时重试
  （否则重放会导致卡片文本重复）；成员卡死由 member_timeout_s 截断；
- 单成员草稿进入提示词前有字符上限截断（DRAFT_CHAR_LIMIT），防止多成员
  长草稿把主席/成员的上下文撑爆；
- CancelledError 不在引擎层吞掉：成员用量在取消时也会累加进 outcome，
  融合取消时已产出的部分文本会保留在 outcome.fused_text 里，由调用方落库。
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
from .effort import resolve_auto_effort

MEMBER_SYSTEM = (
    "你是圆桌讨论的成员之一。请针对用户的问题独立给出你的最佳回答："
    "直接给结论与理由，结构清晰、内容完整。"
    "不要询问其他人的意见，不要反问用户，不要输出与问题无关的客套话。"
)

DEBATE_SYSTEM = (
    "你是圆桌讨论的成员之一。现在进入第二轮讨论：你会看到其他成员对同一问题"
    "的上一轮回答。请吸收其中的正确观点、回应你不同意的分歧，"
    "修订并输出你的最终回答：结构清晰、内容完整、可直接作为最终答案。"
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

# 单成员草稿进入融合/辩论提示词的字符上限：长草稿截断（带标记），
# 防止 8 个成员的长草稿把主席/成员自己的上下文撑爆。
DRAFT_CHAR_LIMIT = 12_000


def clip_draft(text: str) -> str:
    """草稿过长时截断（带省略标记），保护融合/辩论提示词不爆上下文。"""
    if len(text) <= DRAFT_CHAR_LIMIT:
        return text
    return text[:DRAFT_CHAR_LIMIT] + "\n…（草稿过长，已截断）"


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
    text: str = ""  # 最终草稿（最后一次成功作答的文本）
    status: str = "done"  # done | error
    error: str = ""
    input_tokens: int = 0  # 累计（多轮辩论时含各轮；含重试中已上报的部分）
    output_tokens: int = 0

    @property
    def label(self) -> str:
        return f"{self.spec.provider_name}/{self.spec.model}"


@dataclass
class RoundtableOutcome:
    """整场圆桌的结果：成员草稿 + 融合文本与用量。

    status：
    - done：正常走完（融合成功或 compare 模式由调用方分支处理）
    - error：融合失败（成员草稿仍在 members 里，调用方可降级展示）
    - cancelled：用户取消（部分文本保留在 fused_text / 成员 text 里）
    """

    members: list[MemberResult] = field(default_factory=list)
    fused_text: str = ""
    status: str = "done"
    error: str = ""
    input_tokens: int = 0  # 成员轮用量合计
    output_tokens: int = 0
    # 融合轮（主席）的用量单独放：调用方把成员逐条入账、融合记主席名下
    chair_input_tokens: int = 0
    chair_output_tokens: int = 0
    # 主席标识（backend 填，供 usage_rows 与展示）
    chair_provider: str = ""
    chair_model: str = ""


def _label(spec: MemberSpec) -> str:
    return f"{spec.provider_name}/{spec.model}"


def usage_rows(outcome: RoundtableOutcome) -> list[dict]:
    """按成员逐条的用量记录（融合轮记在主席名下），供 usage_log 入账。

    compare 模式没有融合轮，outcome.chair_* 为 0，自然只有成员行。
    """
    rows = [
        {
            "provider": r.spec.provider_name,
            "model": r.spec.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
        }
        for r in outcome.members
        if r.input_tokens or r.output_tokens
    ]
    if outcome.chair_input_tokens or outcome.chair_output_tokens:
        rows.append({
            "provider": outcome.chair_provider or "chair",
            "model": outcome.chair_model,
            "input_tokens": outcome.chair_input_tokens,
            "output_tokens": outcome.chair_output_tokens,
        })
    return rows


async def _run_member(
    spec: MemberSpec,
    index: int,
    history: list[Message],
    user_text: str,
    timeout_s: int,
    emit: EmitFn,
    *,
    round_no: int = 0,
    system_text: str = MEMBER_SYSTEM,
    extra_instruction: str = "",
) -> MemberResult:
    """一个成员一轮作答：重试/超时/容错都收敛在自己身上，失败不影响其他成员。

    history 不含本次提问（由本函数拼在最后），保证成员消息序列规整。
    round_no 用于事件 round 字段（前端区分独立作答轮与辩论修订轮）。
    extra_instruction 非空时（辩论轮）附加在用户问题之后注入其他成员草稿。
    """
    result = MemberResult(spec=spec, index=index)

    async def finish(status: str, error: str = "") -> MemberResult:
        result.status = status
        result.error = error
        await emit(RoundtableMemberFinished(
            member_index=index, status=status, error=error, round=round_no,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        ))
        if result.input_tokens or result.output_tokens:
            await emit(Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens))
        return result

    if spec.provider is None:
        return await finish("error", spec.build_error or "模型服务不可用")

    messages = [Message.system(system_text), *dialogue(history)]
    if extra_instruction:
        messages.append(Message.user(f"{user_text}\n\n---\n\n{extra_instruction}"))
    else:
        messages.append(Message.user(user_text))
    parts: list[str] = []

    async def consume() -> None:
        # 自动档：成员作答同样按任务复杂度实时估档
        async for pe in spec.provider.stream(
            messages, [], effort=resolve_auto_effort(spec.provider, messages),
        ):
            if isinstance(pe, ProviderTextDelta):
                parts.append(pe.text)
                await emit(RoundtableMemberDelta(
                    member_index=index, round=round_no, text=pe.text,
                ))
            elif isinstance(pe, ProviderDone):
                result.input_tokens += pe.input_tokens
                result.output_tokens += pe.output_tokens

    for attempt in range(1, MAX_STREAM_RETRIES + 2):
        parts = []
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


def _drafts_others(results: list[MemberResult], self_index: int) -> str:
    """辩论轮：给 self_index 成员看的其他成员草稿分节文本。"""
    sections: list[str] = []
    for r in results:
        if r.index == self_index:
            continue
        if r.status == "done" and r.text.strip():
            body = clip_draft(r.text.strip())
        else:
            body = f"（该成员作答失败：{r.error or '未知错误'}）"
        sections.append(f"### 成员{r.index + 1}（{r.label}）\n{body}")
    return "\n\n".join(sections)


def drafts_section(results: list[MemberResult]) -> str:
    """把成员草稿排版成融合提示词里的分节文本。"""
    sections: list[str] = []
    for r in results:
        if r.status == "done" and r.text.strip():
            body = clip_draft(r.text.strip())
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
    取消时同样返回已产出的部分文本（error="cancelled"），由调用方决定落库；
    不把 CancelledError 向上抛，避免丢失已流出的内容。
    """
    parts: list[str] = []
    usage_in = usage_out = 0

    async def consume() -> None:
        nonlocal usage_in, usage_out
        async for pe in chair.stream(
            messages, [], effort=resolve_auto_effort(chair, messages),
        ):
            if isinstance(pe, ProviderTextDelta):
                parts.append(pe.text)
                await emit(TextDelta(text=pe.text))
            elif isinstance(pe, ProviderDone):
                usage_in += pe.input_tokens
                usage_out += pe.output_tokens

    error = ""
    try:
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
    except asyncio.CancelledError:
        # 用户取消：已流出的部分文本交给调用方落库
        asyncio.current_task().uncancel()
        return "".join(parts), "cancelled", usage_in, usage_out

    return "".join(parts), error, usage_in, usage_out


async def _run_round(
    members: list[MemberSpec],
    prev: list[MemberResult],
    round_no: int,
    history: list[Message],
    user_text: str,
    timeout_s: int,
    emit: EmitFn,
    skip: set[int] | None = None,
) -> list[MemberResult]:
    """跑一轮成员作答。

    round 0：全员并行独立作答。
    round >= 1（辩论轮）：每个成员读到其他成员的上一轮草稿后修订；
    skip 里的成员已收敛（上一轮修订没有改动文本），直接沿用 prev 结果对象。
    返回本轮各成员的 MemberResult（新列表；跳过的成员是同一个对象）。
    """
    if round_no == 0:
        return list(await asyncio.gather(*(
            _run_member(spec, i, history, user_text, timeout_s, emit)
            for i, spec in enumerate(members)
        )))

    skip = skip or set()

    async def run_one(i: int) -> MemberResult:
        if i in skip:
            # 已收敛：不发 delta、不再花钱；照发一个 skipped 的结束事件，
            # 前端才能按轮结算（否则这轮的「全员结束」永远凑不齐）
            prev_r = prev[i]
            await emit(RoundtableMemberFinished(
                member_index=i, status=prev_r.status, error=prev_r.error,
                round=round_no, skipped=True,
            ))
            return prev_r
        others = _drafts_others(prev, i)
        instruction = (
            f"## 其他成员的上一轮回答\n\n{others}\n\n"
            "请参考以上回答修订你的最终答案。"
        )
        r = await _run_member(
            members[i], i, history, user_text, timeout_s, emit,
            round_no=round_no, system_text=DEBATE_SYSTEM,
            extra_instruction=instruction,
        )
        # 用量跨轮累计：_run_member 每次新建 result，这里把上一轮的量叠上去
        # （finish() 已经按本轮量发过 Usage 事件，事件流仍是逐轮的诚实值）
        if i < len(prev):
            r.input_tokens += prev[i].input_tokens
            r.output_tokens += prev[i].output_tokens
        return r

    return list(await asyncio.gather(*(run_one(i) for i in range(len(members)))))


async def run_roundtable(
    *,
    members: list[MemberSpec],
    chair: Provider,
    system_text: str,
    history: list[Message],
    user_text: str,
    timeout_s: int,
    emit: EmitFn,
    debate_rounds: int = 0,
    fuse: bool = True,
    chair_provider: str = "",
    chair_model: str = "",
) -> RoundtableOutcome:
    """跑一整场圆桌：并行作答（+ 可选辩论）→ 主席融合。

    debate_rounds：额外辩论修订轮数（0=只独立作答；1=成员看彼此草稿后
    修订一次再融合）。上限 2。
    fuse=False（A/B 对比模式）：只跑成员作答，不跑主席融合——各成员草稿
    由调用方直接呈现，省掉一次白跑的融合调用。
    返回完整结果，不直接改写主历史；取消时 outcome.status=cancelled，
    已产出内容保留。
    """
    total_rounds = 1 + max(0, min(int(debate_rounds), 2))
    await emit(RoundtableStarted(
        members=[
            {"index": i, "provider": s.provider_name, "model": s.model}
            for i, s in enumerate(members)
        ],
        rounds=total_rounds,
    ))

    outcome = RoundtableOutcome(members=[])
    outcome.chair_provider = chair_provider
    outcome.chair_model = chair_model
    results: list[MemberResult] = []

    def _settle_usage(cur: list[MemberResult]) -> None:
        """把当前一轮 results 里的用量累计进 outcome（逐轮取 max，防跳过成员重复算）。"""
        outcome.input_tokens = sum(r.input_tokens for r in cur)
        outcome.output_tokens = sum(r.output_tokens for r in cur)

    try:
        prev: list[MemberResult] = []
        # prev_before：上一轮的前一轮草稿文本（判断成员「修订后没改动」用）
        prev_before_texts: list[str | None] | None = None
        for round_no in range(total_rounds):
            skip: set[int] = set()
            if round_no > 0 and prev_before_texts is not None:
                for i in range(len(members)):
                    if i < len(prev) and prev[i].status == "done" \
                            and prev[i].text == prev_before_texts[i]:
                        skip.add(i)  # 上一轮修订后文本没变：已收敛，跳过
            results = await _run_round(
                members, prev, round_no, history, user_text, timeout_s, emit, skip,
            )
            _settle_usage(results)
            # 全员收敛（本轮没有任何成员实际修订）→ 提前结束辩论
            if round_no > 0 and prev and len(results) == len(prev) and all(
                a is b for a, b in zip(results, prev, strict=False)
            ):
                break
            prev_before_texts = (
                [r.text for r in prev] if prev else None
            )
            prev = results
    except asyncio.CancelledError:
        # 取消已在引擎层收敛（保留已产出的内容）；uncancel 消除悬挂的取消状态，
        # 与 backend._run_turn_pipeline 里 shield 路径的写法一致
        asyncio.current_task().uncancel()
        outcome.status = "cancelled"
        outcome.members = results
        return outcome

    outcome.members = results
    ok_drafts = [r for r in results if r.status == "done" and r.text.strip()]
    if not ok_drafts:
        # 一个能用的草稿都没有：融合无意义，直接失败（调用方决定降级展示）
        outcome.status = "error"
        outcome.error = "所有成员均未产出回答"
        return outcome
    if not fuse:
        # A/B 对比模式：草稿即成品，不跑主席融合
        return outcome

    fusion_messages = build_fusion_messages(system_text, history, user_text, results)
    fused, error, usage_in, usage_out = await _run_fusion(
        chair, fusion_messages, max(timeout_s * 2, 300), emit,
    )
    outcome.fused_text = fused
    outcome.chair_input_tokens = usage_in
    outcome.chair_output_tokens = usage_out
    if error == "cancelled":
        outcome.status = "cancelled"
        return outcome
    if usage_in or usage_out:
        await emit(Usage(input_tokens=usage_in, output_tokens=usage_out))

    if error:
        outcome.status = "error"
        outcome.error = error
    return outcome
