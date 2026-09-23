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
  不存在同一 Provider 实例的并发调用；同一 provider_name 的成员共享信号量
  （PER_PROVIDER_CONCURRENCY），避免同一把 API Key 并发多路触发限流；
- 主历史只保留「user(问题) + assistant(融合答案)」，草稿通过事件流与
  消息元数据留存，不进入主历史；
- 瞬态错误复用 agent.py 的判定与重试节奏；只在还没吐出任何内容时重试
  （否则重放会导致卡片文本重复）；成员卡死由 member_timeout_s 截断；
- 草稿截断有两层：单成员 DRAFT_CHAR_LIMIT 字符上限 + 融合/辩论提示词里
  全部草稿的总字符预算（融合按主席剩余上下文估算，见 _fusion_draft_budget），
  防止多成员长草稿把上下文撑爆；
- 辩论轮跳过两类成员：已收敛（上一轮修订没改动文本）不再花钱；
  构建失败（provider 为 None，永远不可能成功）从第一个辩论轮起就跳过；
  运行期失败的成员在第一个辩论轮还有一次带彼此草稿的重试机会
  （瞬态故障可能自愈），连败两轮后不再重试；
- CancelledError 不在引擎层吞掉：成员用量在取消时也会累加进 outcome，
  成员阶段取消时进行中成员已流出的部分草稿会回收进 outcome.members
  （状态 error，用户已看到的内容不凭空消失），融合取消时已产出的部分
  文本保留在 outcome.fused_text 里，由调用方落库。
"""

from __future__ import annotations

import asyncio
import difflib
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
from .context import estimate_text_tokens, estimate_tokens
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

# 成员身份预设：给不同成员注入不同视角的附加提示词，让多样性不只来自模型差异。
# id 随成员参数与圆桌元数据持久化（要保持稳定），值拼在成员系统提示词末尾。
MEMBER_ROLES: dict[str, str] = {
    "": "",  # 普通成员（默认）：无附加身份
    "critic": (
        "你的视角是「批评者」：重点审查常见错误、边界情况与隐患，"
        "敢于指出站不住脚的观点，宁可苛刻也不要客气。"
    ),
    "factcheck": (
        "你的视角是「事实核查员」：优先核对事实、数据与引用是否准确，"
        "对没有把握的内容明确标注「不确定」，不要为了完整而编造。"
    ),
    "concise": (
        "你的视角是「简洁派」：用最少的篇幅给出要点式回答，"
        "能一句话说清就不写一段，不铺陈背景与客套。"
    ),
    "practitioner": (
        "你的视角是「实干者」：优先给出可落地的步骤、命令与示例，"
        "以「照着做就能完成」为标准，而不是原理铺陈。"
    ),
}

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

# 融合/辩论提示词里全部草稿的总字符预算（主席上下文未知时的兜底上限；
# 主席上下文已知时融合预算按剩余空间另算，见 _fusion_draft_budget）。
DRAFTS_TOTAL_CHAR_LIMIT = 48_000
# 预算估算给融合答案留的输出余量（token）。
FUSION_ANSWER_MARGIN_TOKENS = 4_000
# 主席上下文已知但剩余空间极小时，草稿总预算的绝对下限（字符）；实际下限还会
# 随主席上下文放大到 5%（见 _fusion_draft_budget），小上下文不硬塞大预算。
MIN_DRAFTS_BUDGET_CHARS = 2_000
# 等比压缩时单份草稿至少保留的字符；预算连这个都放不下时退化为硬等比。
MIN_KEPT_DRAFT_CHARS = 500

# 同一 provider_name 的成员共享的并发上限：多模型共用一把 API Key 时
# 并发流式过密容易触发限流，而流式吐到一半被 429 是不能重试的
#（重放会导致卡片文本重复），所以从源头限流比事后重试更有效。
PER_PROVIDER_CONCURRENCY = 2

# 近重去重：成员草稿与另一份已保留草稿的相似度（SequenceMatcher 比率）
# 超阈值时，正文替换为「与成员X基本一致」占位，融合/辩论提示词不再逐字
# 读 N 份雷同草稿。只对长草稿做——短文本相似度高是常态、语义还可能相反。
DEDUPE_MIN_CHARS = 600
DEDUPE_SIMILARITY = 0.9


def clip_draft(text: str) -> str:
    """草稿过长时截断（带省略标记），保护融合/辩论提示词不爆上下文。"""
    if len(text) <= DRAFT_CHAR_LIMIT:
        return text
    return text[:DRAFT_CHAR_LIMIT] + "\n…（草稿过长，已截断）"


_DRAFT_CLIP_MARK = "\n…（草稿过长，已截断）"


def clip_drafts_total(texts: list[str], budget: int) -> list[str]:
    """草稿总字符超预算时按长度等比压缩（带截断标记）。

    与 clip_draft 的单份上限互补：clip_draft 管单份，这里管全部草稿加起来
    不超过预算。等比而不是砍尾，保证每个成员的观点都还能被看到。
    """
    total = sum(len(t) for t in texts)
    if budget <= 0 or total <= budget:
        return list(texts)
    scale = budget / total
    clipped = [
        t if len(t) <= (keep := max(MIN_KEPT_DRAFT_CHARS, int(len(t) * scale)))
        else t[:keep] + _DRAFT_CLIP_MARK
        for t in texts
    ]
    over = sum(len(t) for t in clipped) - budget
    if over > 0:
        # MIN_KEPT_DRAFT_CHARS 兜底导致仍超（预算比份数×兜底还小）：硬等比再压一遍
        total2 = sum(len(t) for t in clipped)
        scale2 = max(0.0, 1.0 - over / total2)
        clipped = [
            t if len(t) <= 200
            else t[: max(120, int(len(t) * scale2) - len(_DRAFT_CLIP_MARK))] + _DRAFT_CLIP_MARK
            for t in clipped
        ]
    return clipped


def dedupe_similar_bodies(entries: list[tuple[int, str]]) -> list[str]:
    """近重去重：与已保留草稿高度相似的正文替换为指回占位（输入序不变）。

    entries 是 (成员index, 正文)。按出现顺序保留第一份，后面与它相似度
    超阈值的（辩论收敛后成员草稿趋同是常态）替换为「与成员X基本一致」，
    融合/辩论提示词不再逐字读 N 份雷同草稿。短草稿不参与（相似度高是
    常态、语义还可能相反）；比对走 quick_ratio 上界短路，开销可忽略。
    """
    kept: list[tuple[int, str]] = []
    dup_of: dict[int, int] = {}
    for idx, body in entries:
        dup = None
        if len(body) >= DEDUPE_MIN_CHARS:
            for kidx, kbody in kept:
                sm = difflib.SequenceMatcher(None, body, kbody)
                if sm.real_quick_ratio() < DEDUPE_SIMILARITY:
                    continue
                if sm.quick_ratio() < DEDUPE_SIMILARITY:
                    continue
                if sm.ratio() >= DEDUPE_SIMILARITY:
                    dup = kidx
                    break
        if dup is None:
            kept.append((idx, body))
        else:
            dup_of[idx] = dup
    final: list[str] = []
    for idx, body in entries:
        k = dup_of.get(idx)
        final.append(
            f"（本成员的回答与成员{k + 1}基本一致，不再重复呈现）"
            if k is not None else body
        )
    return final


def recent_turn_history(history: list[Message], turns: int) -> list[Message]:
    """成员轻上下文：只保留最近 turns 轮（每条 user 消息开启一轮）的对话体。

    0（默认）= 全量。主席融合始终吃全量历史——砍的只是成员侧的输入放大
    （N 个成员 × 全量历史是圆桌最大的 token 开销），不砍信息完整性。
    """
    if turns <= 0:
        return history
    msgs = dialogue(history)
    user_pos = [i for i, m in enumerate(msgs) if m.role == "user"]
    if len(user_pos) <= turns:
        return msgs
    return msgs[user_pos[-turns]:]


def _fusion_draft_budget(
    system_text: str, history: list[Message], question: str, chair_context_tokens: int,
) -> int:
    """融合草稿的总字符预算：主席上下文已知时按剩余空间估，未知时用兜底上限。

    折算按 1 token ≈ 1 字符（CJK 实际约 0.7 token/字、西文更省），
    只会少留不会超——超限的代价是整场融合白跑，宁保守。
    剩余空间算出来是负数时（主席上下文很小）按下限截断；下限随上下文
    缩放（5%，绝对下限 MIN_DRAFTS_BUDGET_CHARS），小上下文不再硬塞 8000 字。
    """
    if chair_context_tokens <= 0:
        return DRAFTS_TOTAL_CHAR_LIMIT
    base: list[Message] = []
    if system_text:
        base.append(Message.system(system_text))
    base.extend(dialogue(history))
    base.append(Message.user(question))
    other_est = estimate_tokens(base) + estimate_text_tokens(FUSION_INSTRUCTION)
    room = chair_context_tokens - other_est - FUSION_ANSWER_MARGIN_TOKENS
    floor = max(MIN_DRAFTS_BUDGET_CHARS, chair_context_tokens // 20)
    return min(DRAFTS_TOTAL_CHAR_LIMIT, max(floor, room))


# 发给前端的事件回调（backend 负责把它转成 WS 帧广播）
EmitFn = Callable[[AgentEvent], Awaitable[None]]


@dataclass
class MemberSpec:
    """一个圆桌成员：provider/model 标识 + 已构建的 Provider 实例。"""

    provider_name: str
    model: str
    provider: Provider | None = None  # 构建失败（如缺 Key）时为 None，作答时直接报错
    build_error: str = ""
    role: str = ""  # 身份预设 id（MEMBER_ROLES 键；空 = 普通成员）


@dataclass
class MemberResult:
    spec: MemberSpec
    index: int
    text: str = ""  # 最终草稿（最后一次成功作答的文本）
    status: str = "done"  # done | error
    error: str = ""
    input_tokens: int = 0  # 累计（多轮辩论时含各轮；含重试中已上报的部分）
    output_tokens: int = 0
    cached_tokens: int = 0  # 命中提示词缓存的部分（含在 input_tokens 里）

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
    chair_cached_tokens: int = 0
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
            "cached_tokens": r.cached_tokens,
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
            "cached_tokens": outcome.chair_cached_tokens,
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
    semaphore: asyncio.Semaphore | None = None,
    live_parts: list[str] | None = None,
) -> MemberResult:
    """一个成员一轮作答：重试/超时/容错都收敛在自己身上，失败不影响其他成员。

    history 不含本次提问（由本函数拼在最后），保证成员消息序列规整。
    round_no 用于事件 round 字段（前端区分独立作答轮与辩论修订轮）。
    extra_instruction 非空时（辩论轮）附加在用户问题之后注入其他成员草稿。
    semaphore 是同 provider_name 成员共享的并发闸（见 PER_PROVIDER_CONCURRENCY）。
    live_parts 是调用方持有的共享草稿桶：本函数把流式增量实时写进去，
    取消时调用方能收回已流出的部分文本（与融合路径的取消语义对齐）。
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

    # 身份预设：拼在系统提示词末尾（独立作答轮与辩论轮都带上，视角贯穿全程）
    role_prompt = MEMBER_ROLES.get(spec.role, "")
    if role_prompt:
        system_text = f"{system_text}\n\n{role_prompt}"

    messages = [Message.system(system_text), *dialogue(history)]
    if extra_instruction:
        messages.append(Message.user(f"{user_text}\n\n---\n\n{extra_instruction}"))
    else:
        messages.append(Message.user(user_text))
    # 共享草稿桶：重试只在还没有任何内容时发生，clear() 不会丢已提交的文本
    parts: list[str] = live_parts if live_parts is not None else []

    async def stream_once() -> None:
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
                result.cached_tokens += pe.cached_tokens

    async def consume() -> None:
        if semaphore is not None:
            async with semaphore:
                await stream_once()
        else:
            await stream_once()

    for attempt in range(1, MAX_STREAM_RETRIES + 2):
        parts.clear()
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


def _drafts_others(
    results: list[MemberResult], self_index: int,
    total_char_budget: int | None = None,
) -> str:
    """辩论轮：给 self_index 成员看的其他成员草稿分节文本（先近重去重再压总量）。"""
    entries: list[tuple[int, str]] = []
    others: list[MemberResult] = []
    for r in results:
        if r.index == self_index:
            continue
        others.append(r)
        if r.status == "done" and r.text.strip():
            entries.append((r.index, clip_draft(r.text.strip())))
        else:
            entries.append((r.index, f"（该成员作答失败：{r.error or '未知错误'}）"))
    bodies = dedupe_similar_bodies(entries)
    if total_char_budget is not None:
        bodies = clip_drafts_total(bodies, total_char_budget)
    sections = [
        f"### 成员{r.index + 1}（{r.label}）\n{body}"
        for r, body in zip(others, bodies, strict=True)
    ]
    return "\n\n".join(sections)


def drafts_section(
    results: list[MemberResult], total_char_budget: int | None = None,
) -> str:
    """把成员草稿排版成融合提示词里的分节文本（先近重去重再压总量；标题不计入预算）。"""
    entries: list[tuple[int, str]] = []
    for r in results:
        if r.status == "done" and r.text.strip():
            entries.append((r.index, clip_draft(r.text.strip())))
        else:
            entries.append((r.index, f"（该成员作答失败：{r.error or '未知错误'}）"))
    bodies = dedupe_similar_bodies(entries)
    if total_char_budget is not None:
        bodies = clip_drafts_total(bodies, total_char_budget)
    sections = [
        f"### 成员{r.index + 1}（{r.label}）\n{body}"
        for r, body in zip(results, bodies, strict=True)
    ]
    return "\n\n".join(sections)


def build_fusion_messages(
    system_text: str,
    history: list[Message],
    question: str,
    results: list[MemberResult],
    total_char_budget: int | None = None,
) -> list[Message]:
    """融合轮消息：主历史（不含本次提问）+ 合并了问题/指令/草稿的最终 user 消息。

    把问题并入融合指令（而不是在历史末尾追加第二条 user 消息），
    保证 openai/anthropic 两种协议都不会出现连续同角色消息。
    total_char_budget 是全部草稿的总字符预算（None=不设，仅单份上限生效）。
    """
    msgs: list[Message] = []
    if system_text:
        msgs.append(Message.system(system_text))
    msgs.extend(dialogue(history))
    combined = (
        f"{question}\n\n---\n\n{FUSION_INSTRUCTION}\n\n"
        f"## 各成员独立草稿\n\n{drafts_section(results, total_char_budget)}"
    )
    msgs.append(Message.user(combined))
    return msgs


async def _run_fusion(
    chair: Provider,
    messages: list[Message],
    timeout_s: int,
    emit: EmitFn,
) -> tuple[str, str, int, int, int]:
    """主席融合：流式产出最终答案。返回 (text, error, input_tokens, output_tokens, cached_tokens)。

    失败但已有部分文本时，保留部分文本一并返回（用户已经看到了，落库保持一致）。
    取消时同样返回已产出的部分文本（error="cancelled"），由调用方决定落库；
    不把 CancelledError 向上抛，避免丢失已流出的内容。
    """
    parts: list[str] = []
    usage_in = usage_out = usage_cached = 0

    async def consume() -> None:
        nonlocal usage_in, usage_out, usage_cached
        async for pe in chair.stream(
            messages, [], effort=resolve_auto_effort(chair, messages),
        ):
            if isinstance(pe, ProviderTextDelta):
                parts.append(pe.text)
                await emit(TextDelta(text=pe.text))
            elif isinstance(pe, ProviderDone):
                usage_in += pe.input_tokens
                usage_out += pe.output_tokens
                usage_cached += pe.cached_tokens

    error = ""
    try:
        for attempt in range(1, MAX_STREAM_RETRIES + 2):
            parts = []
            usage_in = usage_out = usage_cached = 0
            retryable = False
            try:
                await asyncio.wait_for(consume(), timeout=timeout_s)
                return "".join(parts), "", usage_in, usage_out, usage_cached
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
        return "".join(parts), "cancelled", usage_in, usage_out, usage_cached

    return "".join(parts), error, usage_in, usage_out, usage_cached


async def _run_round(
    members: list[MemberSpec],
    prev: list[MemberResult],
    round_no: int,
    history: list[Message],
    user_text: str,
    timeout_s: int,
    emit: EmitFn,
    skip: set[int] | None = None,
    semaphores: dict[str, asyncio.Semaphore] | None = None,
    live_parts: dict[int, list[str]] | None = None,
) -> list[MemberResult]:
    """跑一轮成员作答。

    round 0：全员并行独立作答。
    round >= 1（辩论轮）：每个成员读到其他成员的上一轮草稿后修订；
    skip 里的成员已收敛或已确定不再重试，直接沿用 prev 结果对象。
    返回本轮各成员的 MemberResult（新列表；跳过的成员是同一个对象）。
    """
    # 注意不要写成 (live_parts or {})：空字典判假会新建临时 dict，
    # setdefault 就落不到调用方持有的那个字典上了（取消回收会拿不到草稿）
    skip = skip or set()
    semaphores = semaphores or {}
    live_parts = live_parts if live_parts is not None else {}

    if round_no == 0:
        return list(await asyncio.gather(*(
            _run_member(
                spec, i, history, user_text, timeout_s, emit,
                semaphore=semaphores.get(spec.provider_name),
                live_parts=live_parts.setdefault(i, []),
            )
            for i, spec in enumerate(members)
        )))

    async def run_one(i: int) -> MemberResult:
        if i in skip:
            # 已收敛/不再重试：不发 delta、不再花钱；照发一个 skipped 的结束事件，
            # 前端才能按轮结算（否则这轮的「全员结束」永远凑不齐）
            prev_r = prev[i]
            await emit(RoundtableMemberFinished(
                member_index=i, status=prev_r.status, error=prev_r.error,
                round=round_no, skipped=True,
            ))
            return prev_r
        others = _drafts_others(prev, i, total_char_budget=DRAFTS_TOTAL_CHAR_LIMIT)
        instruction = (
            f"## 其他成员的上一轮回答\n\n{others}\n\n"
            "请参考以上回答修订你的最终答案。"
        )
        r = await _run_member(
            members[i], i, history, user_text, timeout_s, emit,
            round_no=round_no, system_text=DEBATE_SYSTEM,
            extra_instruction=instruction,
            semaphore=semaphores.get(members[i].provider_name),
            live_parts=live_parts.setdefault(i, []),
        )
        # 用量跨轮累计：_run_member 每次新建 result，这里把上一轮的量叠上去
        # （finish() 已经按本轮量发过 Usage 事件，事件流仍是逐轮的诚实值）
        if i < len(prev):
            r.input_tokens += prev[i].input_tokens
            r.output_tokens += prev[i].output_tokens
            r.cached_tokens += prev[i].cached_tokens
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
    chair_context_tokens: int = 0,
    member_history_turns: int = 0,
) -> RoundtableOutcome:
    """跑一整场圆桌：并行作答（+ 可选辩论）→ 主席融合。

    debate_rounds：额外辩论修订轮数（0=只独立作答；1=成员看彼此草稿后
    修订一次再融合）。上限 2。
    fuse=False（A/B 对比模式）：只跑成员作答，不跑主席融合——各成员草稿
    由调用方直接呈现，省掉一次白跑的融合调用。
    chair_context_tokens：主席上下文上限（0=未知），用于估算融合草稿总预算。
    member_history_turns：成员轻上下文——成员只看最近 N 轮历史（0=全量）；
    主席融合始终吃全量历史，砍的只是成员侧的输入放大。
    返回完整结果，不直接改写主历史；取消时 outcome.status=cancelled，
    已产出内容（含进行中成员已流出的部分草稿）保留。
    """
    total_rounds = 1 + max(0, min(int(debate_rounds), 2))
    await emit(RoundtableStarted(
        members=[
            {"index": i, "provider": s.provider_name, "model": s.model, "role": s.role}
            for i, s in enumerate(members)
        ],
        rounds=total_rounds,
    ))

    outcome = RoundtableOutcome(members=[])
    outcome.chair_provider = chair_provider
    outcome.chair_model = chair_model
    results: list[MemberResult] = []
    # 同一 provider_name 的成员共享并发闸（多模型一把 Key 时从源头防限流）
    semaphores = {
        name: asyncio.Semaphore(PER_PROVIDER_CONCURRENCY)
        for name in {s.provider_name for s in members}
    }
    # 进行中成员的实时草稿桶：index -> 流式增量（取消时据此回收部分草稿）
    live_parts: dict[int, list[str]] = {}
    # 成员轻上下文：成员吃裁剪后的历史，主席融合吃全量
    #（_fusion_draft_budget 也按全量历史估算，口径一致）
    member_history = recent_turn_history(history, member_history_turns)

    def _settle_usage(cur: list[MemberResult]) -> None:
        """把当前一轮 results 里的用量累计进 outcome（逐轮取 max，防跳过成员重复算）。"""
        outcome.input_tokens = sum(r.input_tokens for r in cur)
        outcome.output_tokens = sum(r.output_tokens for r in cur)

    try:
        prev: list[MemberResult] = []
        # prev_before：上一轮的前一轮草稿文本（判断成员「修订后没改动」用）
        prev_before_texts: list[str | None] | None = None
        fail_counts: dict[int, int] = {}  # 各成员累计失败轮数（辩论轮跳过判定用）
        for round_no in range(total_rounds):
            skip: set[int] = set()
            if round_no > 0:
                for i in range(len(members)):
                    if i >= len(prev):
                        continue
                    if members[i].provider is None:
                        skip.add(i)  # 构建失败：永远不可能成功，零成本跳过
                    elif prev[i].status == "error" and fail_counts.get(i, 0) >= 2:
                        skip.add(i)  # 已连败两轮：辩论轮不再逐轮重试
                    elif prev_before_texts is not None and prev[i].status == "done" \
                            and prev[i].text == prev_before_texts[i]:
                        skip.add(i)  # 上一轮修订后文本没变：已收敛，跳过
            results = await _run_round(
                members, prev, round_no, member_history, user_text, timeout_s, emit,
                skip, semaphores, live_parts,
            )
            for r in results:
                if r.status == "error":
                    fail_counts[r.index] = fail_counts.get(r.index, 0) + 1
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
        recovered = list(results)
        for i, plist in live_parts.items():
            text = "".join(plist)
            if not text.strip():
                continue
            existing = next((r for r in recovered if r.index == i), None)
            if existing is None:
                # 独立作答轮被取消：该成员没有已完成的轮次，部分草稿是唯一产出
                recovered.append(MemberResult(
                    spec=members[i], index=i, text=text,
                    status="error", error="已取消（保留部分草稿）",
                ))
            elif not existing.text.strip() and existing.status == "error":
                # 上一轮失败的成员本轮已流出部分修订：部分草稿比空失败有信息量
                existing.text = text
                existing.error = "已取消（保留部分草稿）"
        recovered.sort(key=lambda r: r.index)
        outcome.members = recovered
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

    fusion_budget = _fusion_draft_budget(system_text, history, user_text, chair_context_tokens)
    fusion_messages = build_fusion_messages(
        system_text, history, user_text, results, total_char_budget=fusion_budget,
    )
    fused, error, usage_in, usage_out, usage_cached = await _run_fusion(
        chair, fusion_messages, max(timeout_s * 2, 300), emit,
    )
    outcome.fused_text = fused
    outcome.chair_input_tokens = usage_in
    outcome.chair_output_tokens = usage_out
    outcome.chair_cached_tokens = usage_cached
    if error == "cancelled":
        outcome.status = "cancelled"
        return outcome
    if usage_in or usage_out:
        await emit(Usage(input_tokens=usage_in, output_tokens=usage_out))

    if error:
        outcome.status = "error"
        outcome.error = error
    return outcome
