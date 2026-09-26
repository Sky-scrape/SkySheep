"""Agent 核心循环。

一次 run_turn() 的完整流程（事件流驱动）：

    用户输入 → [模型流式响应 → 工具调用 →（权限确认）→ 执行 → 结果回传] × N → 最终回复

设计约定：
- 所有过程以 AgentEvent 产出，CLI / GUI / 未来的 WebSocket server 消费同一套事件；
- 敏感操作通过 PermissionGate 授权：authorize() 返回 PendingPermission 时，
  循环产出 PermissionRequest 事件并挂起，前端调用 respond_permission() 恢复；
- 历史保存在 self.history（归一化 Message），与会话存储无缝对接。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

from ..events import (
    AgentEvent,
    AssistantMessage,
    ErrorEvent,
    NoticeEvent,
    PermissionRequest,
    PermissionResolved,
    ScheduleUpdated,
    TextDelta,
    ThinkingDelta,
    TodoUpdated,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)
from ..messages import ContentBlock, ImageBlock, Message, TextBlock, ThinkingBlock, ToolUseBlock
from ..models.base import (
    Provider,
    ProviderDone,
    ProviderReasoning,
    ProviderTextDelta,
    ProviderToolUse,
)
from ..security.gate import (
    Decision,
    PendingPermission,
    PermissionGate,
    normalize_decision,
)
from ..tools.base import Safety, Tool, ToolContext, ToolError, ToolRegistry, truncate_output
from .context import compact_history, estimate_tokens
from .effort import resolve_auto_effort

MAX_TOOL_PREVIEW = 500

# turn 内模型调用失败自动重试：只针对瞬态错误，且仅在还没吐出任何内容时
# （否则重放会导致文本重复，只能如实报错）。对标 Claude Code / Codex 的
# 自动重试行为。
MAX_STREAM_RETRIES = 3
RETRY_BASE_DELAY_S = 1.5
TRANSIENT_MARKERS = (
    "429", "rate limit", "rate_limit", "too many requests",
    "timeout", "timed out", "temporarily unavailable", "overloaded", "overloaded_error",
    "502", "503", "504", "bad gateway", "service unavailable",
    "connection error", "connection reset", "connection aborted",
    "connection closed", "eof occurred", "incomplete", "apitimeout",
)


def is_transient_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(marker in msg for marker in TRANSIENT_MARKERS)


# 上游因为「提示词超出上下文窗口」而拒绝时的特征串。这类错误重试无用，
# 但用户看到的往往是一句英文 400，不知道该做什么——所以补一句可操作的中文提示。
CONTEXT_OVERFLOW_MARKERS = (
    "context length", "context_length", "maximum context", "max context",
    "too many tokens", "reduce the length", "prompt is too long",
    "exceed context", "上下文长度", "上下文超", "超过最大长度", "token 数量超过",
)


def is_context_overflow(e: Exception) -> bool:
    msg = str(e).lower()
    return any(marker in msg for marker in CONTEXT_OVERFLOW_MARKERS)


class Agent:
    def __init__(
        self,
        *,
        provider: Provider,
        registry: ToolRegistry,
        gate: PermissionGate,
        working_dir: Path | None,
        max_iterations: int = 40,
        context_limit_tokens: int = 1_000_000,
        compaction_keep_recent: int = 8,
        compaction_trigger: float = 0.9,
        compaction_auto: bool = True,
        hooks=None,
        restrict_to_workdir: bool = False,
        session_id: str = "",
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.gate = gate
        self.working_dir = working_dir
        self.max_iterations = max_iterations
        self.context_limit_tokens = context_limit_tokens
        self.compaction_keep_recent = compaction_keep_recent
        # 占用达到上限的这个比例就触发压缩（0.9 = 留 10% 余量）：顶满才压缩
        # 容易半路撞上上游 400，见 run_turn 开头的两处检查
        self.compaction_trigger = min(0.98, max(0.5, float(compaction_trigger)))
        # 自动压缩总开关（默认开）：关闭后循环里两处自动检查全部跳过，只保留
        # 手动 /compact（backend 直接调 compact_history，不受此开关约束）
        self.compaction_auto = bool(compaction_auto)
        self.hooks = hooks  # core.hooks.HookRunner | None：工具调用前后用户钩子
        self.restrict_to_workdir = restrict_to_workdir
        # 会话 id：透传给钩子命令的 stdin JSON（多会话场景钩子可区分来源）
        self.session_id = session_id
        self.history: list[Message] = []
        self._pending: dict[str, PendingPermission] = {}
        self.total_in_tokens = 0
        self.total_out_tokens = 0
        # 累计命中提示词缓存的 token（分母用 total_in_tokens：provider 已把
        # input_tokens 归一为含缓存部分的完整提示词）。用于界面「平均缓存命中率」。
        self.total_cached_tokens = 0
        # 最近一次模型调用上报的真实输入 token（即那一次请求的整体提示词长度）。
        # 估算再准也有偏差（各家中转的 tokenizer 不同），用它给估算值兜一个下限，
        # 保证界面显示的占用与自动压缩的判断都不会比真实情况乐观。
        self.last_prompt_tokens = 0
        # 全量 token 估算的指纹缓存（见 used_context_tokens）
        self._ctx_cache_fp: tuple | None = None
        self._ctx_cache = 0

    # ---- 状态管理 ----

    def used_context_tokens(self) -> int:
        """当前上下文占用（估算与真实上报取大者，见 last_prompt_tokens）。

        估算带指纹缓存：历史是「只追加 / 整体替换」的结构，(列表 id, 长度,
        末条 id) 不变就复用上次的全量估算——此前每次调用都把整段历史 join
        成大字符串再逐字符正则，长会话每轮的压缩复查与界面明细要白算好几遍。
        set_system 原地替换 system 消息不改这三样，单独置脏。
        """
        fp = (
            id(self.history),
            len(self.history),
            id(self.history[-1]) if self.history else 0,
        )
        if self._ctx_cache_fp != fp:
            self._ctx_cache = estimate_tokens(self.history)
            self._ctx_cache_fp = fp
        return max(self._ctx_cache, self.last_prompt_tokens)

    def set_system(self, text: str) -> None:
        self._ctx_cache_fp = None
        if self.history and self.history[0].role == "system":
            self.history[0] = Message.system(text)
        else:
            self.history.insert(0, Message.system(text))

    def load_history(self, messages: list[Message]) -> None:
        self.history = list(messages)

    def respond_permission(self, request_id: str, decision: str) -> bool:
        """前端对 PermissionRequest 的回复。返回是否成功投递。

        decision 先过白名单再投递：认不出来的值按拒绍处理。不能直接透传——
        主循环只显式处理 ALLOW_ALWAYS 与 DENY，其余一律落到执行工具，
        也就是说透传等于「乱码 = 放行」。
        """
        pending = self._pending.get(request_id)
        if pending is None:
            return False
        pending.resolve(normalize_decision(decision))
        return True

    # ---- 主循环 ----

    def repair_dangling_tool_uses(self) -> list[str]:
        """给「有 tool_use 无 tool_result」的历史补一条中断说明，返回补过的 id。

        取消/异常会把工具循环截断在中间：assistant 消息里的 tool_use 已经进了
        历史，配对的 tool_result 没来得及追加。Anthropic 协议要求每个 tool_use
        都有对应 tool_result（缺一个下一次调用直接 400；OpenAI 兼容层宽松些，
        但语义同样不清）。轮首调用一次让历史自洽：被中断的调用按 is_error 结果
        补上，模型据此知道那次没跑完（安全审查 M12）。
        """
        fixed: list[str] = []
        out: list[Message] = []
        i = 0
        total = len(self.history)
        while i < total:
            msg = self.history[i]
            out.append(msg)
            uses = msg.tool_uses if msg.role == "assistant" else []
            if not uses:
                i += 1
                continue
            # 紧跟其后的 tool 消息（已补过的也在内）：收集已有的结果 id
            j = i + 1
            got: set[str] = set()
            while j < total and self.history[j].role == "tool":
                for blk in self.history[j].content:
                    tid = getattr(blk, "tool_use_id", "")
                    if tid:
                        got.add(tid)
                out.append(self.history[j])
                j += 1
            for tu in uses:
                if tu.id not in got:
                    out.append(Message.tool_result(
                        tu.id,
                        "interrupted: this tool call did not finish "
                        "(turn cancelled or crashed before it returned).",
                        is_error=True,
                    ))
                    fixed.append(tu.id)
            i = j
        if fixed:
            self.history = out
        return fixed

    def clear_pending(self) -> None:
        """清掉所有未决的权限请求（轮次结束/被取消时调用）。

        取消后弹窗可能还留在屏上，但那一轮已经结束了：残留的 request_id 再被
        respond_permission 投递会「成功」，前端以为决策生效（安全审查 M12）。
        """
        self._pending.clear()

    async def run_turn(
        self, user_text: str, images: list[ImageBlock] | None = None,
        append_user: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """一轮对话：追加用户消息 → 跑主循环 → 收尾清未决权限。

        主体在 _turn_events 里，这里只做「包一层」：把轮次前后的收尾放 finally，
        无论正常结束、异常还是被取消（含生成器在 yield 处被关闭）都会走到。
        """
        # 历史自洽：上一轮被取消时可能留下「有 tool_use 无 tool_result」的断口。
        # 必须在追加新用户消息之前补（Anthropic 协议要求 tool_result 紧跟对应的
        # assistant tool_use，顺序反了同样 400）——安全审查 M12
        self.repair_dangling_tool_uses()
        # append_user=False：重新生成——用户消息已在历史末尾，直接重跑这一轮
        if append_user:
            self.history.append(Message.user(user_text, images=images))
        try:
            async for ev in self._turn_events():
                yield ev
        finally:
            # 轮次结束（正常/取消/异常）后未决权限不再有意义：清掉，避免
            # 「取消后残留可再成功投递决策」（安全审查 M12）
            self.clear_pending()

    async def _turn_events(self) -> AsyncIterator[AgentEvent]:
        ctx = ToolContext(
            working_dir=self.working_dir,
            supports_vision=getattr(self.provider, "supports_vision", True),
            restrict_to_workdir=self.restrict_to_workdir,
            session_id=self.session_id,
        )
        iterations = 0
        stop_reason = "max_iterations"  # 正常结束时在 break 前改为 end_turn

        # 本轮墙钟耗时：轮末盖在最后一条助手消息上（前端刷新后仍能显示「用时 X」）。
        # 用 monotonic 而非 time.time：系统时间被 NTP 校准/手动调整时不受影响。
        turn_t0 = time.monotonic()

        finished_by_error = False

        # 0. 上下文压缩：占用达到「上限 × 触发比例」时，先用摘要替换旧历史
        #    （默认 0.9：留出余量，避免顶满上限时才压缩、半路撞上游 400）
        #    compaction_auto=False（设置里关掉自动压缩）时整段跳过
        if self.compaction_auto and \
                self.used_context_tokens() > self.context_limit_tokens * self.compaction_trigger:
            ev = await compact_history(self, keep_recent=self.compaction_keep_recent)
            if ev is not None:
                yield ev
        # 已尽力压缩仍超限（历史太短/摘要失败）：本轮不再逐迭代重试
        compaction_stuck = not self.compaction_auto

        for iteration in range(1, self.max_iterations + 1):
            iterations = iteration
            yield TurnStarted(iteration=iteration)

            # turn 内压缩复查：单轮内工具结果可膨胀数十 k token，只在开头
            # 查一次会半路爆窗（上游 400 拒绝，前面迭代烧掉的费用全部作废）。
            # 每次模型调用前复查一次；帮不上忙时置位，避免白算 O(n) 估算。
            if iteration > 1 and not compaction_stuck \
                    and self.used_context_tokens() > self.context_limit_tokens * self.compaction_trigger:
                ev = await compact_history(self, keep_recent=self.compaction_keep_recent)
                if ev is not None:
                    yield ev
                else:
                    compaction_stuck = True

            # 1. 流式调用模型：瞬态错误（限流/超时/断流）自动重试，
            #    但只要已经吐出过任何内容就不再重放（避免文本重复）
            blocks: list[ContentBlock] = []
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            reasoning_sig = ""
            # 本次模型调用的推理起止时刻（首个/最后一个 thinking 增量）
            think_t0: float | None = None
            think_t1: float | None = None
            for attempt in range(1, MAX_STREAM_RETRIES + 2):
                blocks, text_parts, reasoning_parts, reasoning_sig = [], [], [], ""
                think_t0 = think_t1 = None
                try:
                    # 自动档：每次调用前按当前上下文实时估档（简单任务降、调试设计升），
                    # 其余档位不覆盖、沿用用户所选（见 core/effort.py）
                    async for pe in self.provider.stream(
                        self.history, self.registry.schemas(),
                        effort=resolve_auto_effort(self.provider, self.history),
                    ):
                        if isinstance(pe, ProviderTextDelta):
                            text_parts.append(pe.text)
                            yield TextDelta(text=pe.text)
                        elif isinstance(pe, ProviderReasoning):
                            if pe.text:
                                # 只统计真正有内容的增量：签名块的空文本增量不算思考时间
                                now = time.monotonic()
                                if think_t0 is None:
                                    think_t0 = now
                                think_t1 = now
                                reasoning_parts.append(pe.text)
                                yield ThinkingDelta(text=pe.text)
                            if pe.signature:
                                reasoning_sig = pe.signature
                        elif isinstance(pe, ProviderToolUse):
                            blocks.append(ToolUseBlock(id=pe.id, name=pe.name, input=pe.input))
                        elif isinstance(pe, ProviderDone):
                            if pe.input_tokens or pe.output_tokens:
                                self.total_in_tokens += pe.input_tokens
                                self.total_out_tokens += pe.output_tokens
                                if pe.cached_tokens:
                                    self.total_cached_tokens += pe.cached_tokens
                                if pe.input_tokens:
                                    self.last_prompt_tokens = pe.input_tokens
                                yield Usage(
                                    input_tokens=pe.input_tokens, output_tokens=pe.output_tokens
                                )
                    break  # 本轮调用正常完成
                except Exception as e:
                    got_content = bool(text_parts or reasoning_parts or blocks)
                    if got_content or attempt > MAX_STREAM_RETRIES or not is_transient_error(e):
                        finished_by_error = True
                        if is_context_overflow(e):
                            yield ErrorEvent(
                                message=(
                                    f"{e}\n\n当前模型的上下文已满。可以在输入框执行 /compact "
                                    "压缩历史，或在设置 · 模型服务里把该服务的「上下文上限」"
                                    "调成模型真实的窗口大小。"
                                )
                            )
                        else:
                            yield ErrorEvent(message=str(e))
                        break
                    delay = min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), 10.0)
                    yield NoticeEvent(
                        message=(
                            f"模型调用失败（{str(e)[:160]}），"
                            f"{delay:.0f} 秒后自动重试（第 {attempt}/{MAX_STREAM_RETRIES} 次）…"
                        )
                    )
                    await asyncio.sleep(delay)

            if finished_by_error:
                break

            # 2. 助手消息并入历史（推理块 → 文本块 → 工具调用）
            if text_parts:
                blocks.insert(0, TextBlock(text="".join(text_parts)))
            if reasoning_parts:
                think_ms = 0
                if think_t0 is not None and think_t1 is not None:
                    think_ms = int((think_t1 - think_t0) * 1000)
                blocks.insert(0, ThinkingBlock(
                    text="".join(reasoning_parts), signature=reasoning_sig,
                    duration_ms=think_ms,
                ))
            assistant = Message.assistant(blocks)
            # 最终回答（无后续工具调用）盖本轮实测耗时，供历史恢复展示；
            # 中间迭代不盖（那是过程消息，不是用户等待的交付物）。
            is_final = not assistant.tool_uses
            if is_final:
                assistant.duration_ms = int((time.monotonic() - turn_t0) * 1000)
            self.history.append(assistant)
            yield AssistantMessage(message=assistant.model_dump())

            if is_final:
                stop_reason = "end_turn"
                break

            # 3. 处理工具调用：连续只读工具并发执行（模型一轮常发多个独立读调用，
            #    串行白耗时间）；涉及确认/写入/执行/有顺序依赖的保持串行。
            #    事件仍按原顺序产出，前端观感不变。
            i = 0
            tool_uses = assistant.tool_uses
            while i < len(tool_uses):
                tu = tool_uses[i]
                tool = self.registry.get(tu.name)
                if tool is None:
                    self.history.append(
                        Message.tool_result(tu.id, "unknown tool: " + tu.name, is_error=True)
                    )
                    yield ToolCallStarted(tool_call_id=tu.id, name=tu.name, input=tu.input)
                    yield ToolCallFinished(
                        tool_call_id=tu.id, name=tu.name, preview="unknown tool",
                        is_error=True, duration_ms=0,
                    )
                    i += 1
                    continue

                # 3a. 权限确认（READONLY 或白名单命中时 authorize 返回 None）
                pending = await self.gate.authorize(tool, tu.input)
                if pending is not None:
                    self._pending[pending.request_id] = pending
                    # 预告「总是允许」将写入的规则：与落库共用同一个对象，
                    # 确认弹窗显示的范围就是之后实际生效的范围
                    always_rule = pending.always_rule or self.gate.rule_for(
                        tool, tu.input, self.gate.working_dir)
                    yield PermissionRequest(
                        request_id=pending.request_id,
                        tool_name=tu.name,
                        input=tu.input,
                        safety=pending.safety.value,
                        detail=pending.detail,
                        diff=pending.diff,
                        note=pending.note,
                        rule_kind=always_rule.kind,
                        rule_pattern=always_rule.pattern,
                    )
                    try:
                        decision = await pending.wait()
                    finally:
                        # 取消/异常也要摘掉：残留的 request_id 之后还能被
                        # respond_permission 投递成功，等于给一个已经结束的轮次
                        # 补决策（安全审查 M12：取消后残留可再「成功投递」）
                        self._pending.pop(pending.request_id, None)
                    yield PermissionResolved(request_id=pending.request_id, decision=decision)

                    if decision == Decision.ALLOW_ALWAYS:
                        await self.gate.persist_rule(always_rule)
                    if decision == Decision.DENY:
                        self.history.append(
                            Message.tool_result(
                                tu.id,
                                pending.deny_note or "User denied this operation.",
                                is_error=True,
                            )
                        )
                        yield ToolCallStarted(tool_call_id=tu.id, name=tu.name, input=tu.input)
                        yield ToolCallFinished(
                            tool_call_id=tu.id, name=tu.name, preview="denied by user",
                            is_error=True, duration_ms=0,
                        )
                        i += 1
                        continue

                # 3b. pre 钩子：用户配置的工具调用前检查（退出码 2 / decision=block 阻止）
                if self.hooks is not None and self.hooks.has_pre:
                    block_reason = await self.hooks.run_pre(
                        tu.name, tu.input, session_id=self.session_id
                    )
                    if block_reason:
                        self.history.append(
                            Message.tool_result(
                                tu.id, "blocked by pre_tool_use hook: " + block_reason,
                                is_error=True,
                            )
                        )
                        yield ToolCallStarted(tool_call_id=tu.id, name=tu.name, input=tu.input)
                        yield ToolCallFinished(
                            tool_call_id=tu.id, name=tu.name,
                            preview="blocked by hook: " + block_reason,
                            is_error=True, duration_ms=0,
                        )
                        i += 1
                        continue

                # 并发窗口：从这里起收集连续的免确认只读工具一起跑
                if tool.safety == Safety.READONLY:
                    batch: list[tuple[ToolUseBlock, Tool, dict]] = [(tu, tool, {})]
                    j = i + 1
                    while j < len(tool_uses):
                        tu2 = tool_uses[j]
                        t2 = self.registry.get(tu2.name)
                        if t2 is None or t2.safety != Safety.READONLY:
                            break
                        # pre 钩子存在时不并发（每个调用都可能被阻断，语义复杂化）
                        if self.hooks is not None and self.hooks.has_pre:
                            break
                        batch.append((tu2, t2, {}))
                        j += 1
                    if len(batch) > 1:
                        # 先按序发 started 事件，再并发执行
                        for b_tu, _b_tool, _ in batch:
                            yield ToolCallStarted(
                                tool_call_id=b_tu.id, name=b_tu.name, input=b_tu.input
                            )
                        results = await asyncio.gather(*(
                            self._exec_tool(b_tu, b_tool, ctx) for b_tu, b_tool, _ in batch
                        ))
                        for (b_tu, b_tool, _), (result, is_error, duration_ms, diff) in zip(
                            batch, results, strict=True
                        ):
                            self.history.append(
                                Message.tool_result(
                                    b_tu.id, truncate_output(result), is_error=is_error
                                )
                            )
                            # 并发批里同样处理图片与清单/日程事件（与串行路径一致）
                            attached: list[ImageBlock] = []
                            if not is_error and ctx.images:
                                attached = list(ctx.images)
                                ctx.images.clear()
                                self.history.append(
                                    Message.user(
                                        "[screenshot] 上述工具附带以下屏幕截图（模型可直接查看）。",
                                        images=attached,
                                    )
                                )
                            diff = diff if not is_error else ""
                            yield ToolCallFinished(
                                tool_call_id=b_tu.id,
                                name=b_tu.name,
                                preview=truncate_output(result, MAX_TOOL_PREVIEW),
                                diff=diff,
                                images=[b.model_dump() for b in attached],
                                is_error=is_error,
                                duration_ms=duration_ms,
                            )
                            if not is_error and b_tu.name == "todo_write" and hasattr(b_tool, "items"):
                                yield TodoUpdated(items=list(b_tool.items))
                            if not is_error and b_tu.name == "schedule_write":
                                yield ScheduleUpdated()
                        i = j
                        continue

                # 3c. 串行执行（单个只读或需确认/写入/执行的工具）
                yield ToolCallStarted(tool_call_id=tu.id, name=tu.name, input=tu.input)
                result, is_error, duration_ms, diff = await self._exec_tool(tu, tool, ctx)

                # 3d. post 钩子：仅通知，不影响工具结果
                if self.hooks is not None and self.hooks.post_rules:
                    try:
                        note = await self.hooks.run_post(
                            tu.name, tu.input, session_id=self.session_id
                        )
                        if note:
                            yield NoticeEvent(message=note)
                    except Exception:  # noqa: BLE001 - post 钩子失败不阻断主流程
                        pass

                self.history.append(
                    Message.tool_result(tu.id, truncate_output(result), is_error=is_error)
                )
                # 工具产生的图片（screenshot）：作为 user 消息并入历史，模型才能看到；
                # 同一事件带给前端内联展示
                attached: list[ImageBlock] = []
                if not is_error and ctx.images:
                    attached = list(ctx.images)
                    ctx.images.clear()
                    self.history.append(
                        Message.user(
                            "[screenshot] 上述工具附带以下屏幕截图（模型可直接查看）。",
                            images=attached,
                        )
                    )
                diff = diff if not is_error else ""
                yield ToolCallFinished(
                    tool_call_id=tu.id,
                    name=tu.name,
                    preview=truncate_output(result, MAX_TOOL_PREVIEW),
                    diff=diff,
                    images=[b.model_dump() for b in attached],
                    is_error=is_error,
                    duration_ms=duration_ms,
                )
                if not is_error and tu.name == "todo_write" and hasattr(tool, "items"):
                    yield TodoUpdated(items=list(tool.items))
                if not is_error and tu.name == "schedule_write":
                    yield ScheduleUpdated()
                i += 1

        if finished_by_error:
            stop_reason = "error"
        if stop_reason == "max_iterations":
            # 到顶不是「说完了」：明确给出停止原因（安全审查 M12——旧实现静默断头，
            # 前端看起来像回答被截断，用户与模型都不知道还能继续）
            yield NoticeEvent(message=(
                f"已达到本轮迭代上限（{self.max_iterations} 次工具循环），"
                "已停止继续调用工具。需要继续的话直接回复一条消息即可接着做。"
            ))
        # 任务完成钩子：一轮以最终回答结束（不再调工具、没出错）时触发。
        # 与 post 同姿态仅通知不阻断；「跑完弹提醒」这类用途不再需要逐个 match 工具。
        if stop_reason == "end_turn" and self.hooks is not None and self.hooks.has_stop:
            try:
                note = await self.hooks.run_stop(session_id=self.session_id)
                if note:
                    yield NoticeEvent(message=note)
            except Exception:  # noqa: BLE001 - stop 钩子失败不影响本轮收尾
                pass
        yield TurnFinished(
            stop_reason=stop_reason, iterations=iterations,
            duration_ms=int((time.monotonic() - turn_t0) * 1000),
        )

    async def _exec_tool(
        self, tu: ToolUseBlock, tool: Tool, ctx: ToolContext,
    ) -> tuple[str, bool, int, str]:
        """执行单个工具，返回 (结果文本, 是否出错, 耗时ms, diff)。不产出事件、不写历史。

        diff 取自 ctx.last_diff（写工具在执行中写入，调用前先清空）：
        按调用取回而不是读工具实例属性——工具实例会被并行任务共享，
        实例属性会把上一个任务的 diff 错配给当前任务。

        写工具执行前经权限门领写租约（并行任务写同一文件的协调，见
        security/leases.py），finally 里归还；租约带出的冲突注记追加进
        结果文本，模型与用户都看得到这次并行写入。
        """
        t0 = time.monotonic()
        ctx.last_diff = ""
        lease = None
        if tool.safety != Safety.READONLY:
            try:
                lease = await self.gate.claim_write(tool, tu.input, owner=ctx.session_id)
            except Exception:  # noqa: BLE001 - 租约是协调不是闸门，故障不挡执行
                lease = None
        try:
            try:
                args = tool.args_model.model_validate(tu.input)
                result = await tool.run(args, ctx)
                is_error = False
            except ToolError as e:
                result = str(e)
                is_error = True
            except Exception as e:  # 参数校验失败或工具内部异常
                result = f"tool failed: {type(e).__name__}: {e}"
                is_error = True
        finally:
            note = lease.release() if lease is not None else ""
        if note and not is_error:
            result = result + "\n\n" + note
        duration_ms = int((time.monotonic() - t0) * 1000)
        return result, is_error, duration_ms, ("" if is_error else ctx.last_diff)
