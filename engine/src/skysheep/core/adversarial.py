"""对抗：多模型对抗性审查（四角色流水线，docs/对抗模式设计.md）。

与圆桌（core/roundtable.py，会诊融合）、团队（core/team.py，分工协作）并列的
第三种多模型协作形态。一次对抗轮的流程（事件流驱动，与圆桌同机制）：

    AdversarialStarted(roles=四角色名册)
      → 阶段一 发现者：对目标内容穷尽式扫描（召回优先，宁滥勿缺），
        输出结构化问题清单（JSON），逐条发 AdversarialFindingProposed
      → 阶段二 调查员：假设每条问题不成立、寻找推翻证据（精确优先），
        逐条发 AdversarialVerdict（confirmed / refuted / partial）
      → 阶段三 建议者：仅对成立/部分成立的问题给出修复方案
      → 阶段四 裁判：过滤噪音、按实际影响排序，流式产出最终审查报告
        （TextDelta，随主历史落库，与圆桌融合文本同姿态）
      → AdversarialFinished(status, summary)

设计约定（沿圆桌先例）：
- 全程不传工具表（tool_schemas=[]），不经过 PermissionGate：纯文本协作，
  无新增安全面；被审目标是文本（本轮消息 + 会话上下文），不是工作区扫描
  （那是普通轮/子代理的事）；
- 上下文隔离：四个阶段各自独立构建消息（角色系统提示词 + 本阶段材料），
  阶段间不共享 history——调查员若先读到发现者的推理链，就会顺着对方的
  框架走；对抗的价值恰恰在于两套独立判断的碰撞；
- 不对称激励：发现者鼓励过度报告、调查员鼓励推翻——噪音在流水线中被
  物理过滤，而不是靠「请只报告严重问题」这类在复杂工程代码面前脆弱的
  提示词约束；
- 调查/建议各合并为一次调用（问题逐条编号进提示词、逐条裁决出 JSON），
  成本与问题数解耦（同圆桌「一轮一次作答」的成本观）；
- 角色可由同一模型承担：角色差异来自系统提示词与阶段材料，不要求四个
  不同服务；缺省裁判用当前主模型（同圆桌主席），进攻/防守角色从成员解析
  （成员不足时按序复用同一个，见 backend 的角色映射）；
- 瞬态错误复用 agent.py 的判定与重试节奏；JSON 解析失败追加一次「格式
  纠错」重试（把原始回复与错误回喂给模型自己修），再失败按失败隔离降级：
  发现失败→整场如实报错（没有问题清单，后续阶段无从谈起）；调查失败→
  涉事问题标 pending（悬而未决，不冒充成立也不冒充推翻）；建议失败→方案
  留空。单阶段失败不拖垮其余阶段（裁判照常出报告）；
- 取消语义与圆桌对齐：_run_text_turn 把 CancelledError 收敛成
  error="cancelled"（uncancel 消除悬挂取消状态），已裁决结论照常保留；
  裁判阶段取消时已流出的部分报告文本保留在 outcome.report，由调用方落库。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..events import (
    AdversarialFindingProposed,
    AdversarialFinished,
    AdversarialPhase,
    AdversarialStarted,
    AdversarialVerdict,
    AgentEvent,
    NoticeEvent,
    TextDelta,
)
from ..messages import Message, TextBlock, dialogue
from ..models.base import Provider, ProviderDone, ProviderTextDelta
from .agent import MAX_STREAM_RETRIES, RETRY_BASE_DELAY_S, is_transient_error
from .effort import resolve_auto_effort
from .roundtable import MemberSpec, clip_draft

logger = logging.getLogger(__name__)

# 发给前端的事件回调（与圆桌同一姿态：backend 负责转成 WS 帧广播）
EmitFn = Callable[[AgentEvent], Awaitable[None]]

# ---- 裁决词汇 ----
VERDICT_CONFIRMED = "confirmed"   # 调查员确认成立
VERDICT_REFUTED = "refuted"       # 调查员推翻（误报）
VERDICT_PARTIAL = "partial"       # 部分成立（表述/严重度已被修正）
VERDICT_PENDING = "pending"       # 调查阶段失败/取消：悬而未决，如实呈现
_VALID_VERDICTS = {VERDICT_CONFIRMED, VERDICT_REFUTED, VERDICT_PARTIAL}
_VALID_SEVERITIES = {"critical", "high", "medium", "low"}

# 阶段名（AdversarialPhase.phase 的词汇；前端按此渲染进度）
PHASE_FINDER = "finder"
PHASE_INVESTIGATOR = "investigator"
PHASE_ADVISOR = "advisor"
PHASE_JUDGE = "judge"

# ---- 角色系统提示词（不对称激励是本模式的核心机制，措辞即功能） ----

FINDER_SYSTEM = (
    "你是一场对抗性审查中的发现者（进攻方）。你的唯一任务是穷尽式地找出"
    "被审内容中尽可能多的问题：逻辑 Bug、安全隐患、性能隐患、健壮性缺口、"
    "设计缺陷。宁可错报，不可漏报——你提出的每一条候选问题都会交给一个"
    "立场相反的调查员独立复核，误报会被推翻，漏报则无法挽回。不要自我审查，"
    "不要因为「可能不重要」而略过；用与用户一致的语言写作。"
)

INVESTIGATOR_SYSTEM = (
    "你是一场对抗性审查中的调查员（防守方）。发现者提出了一批候选问题，"
    "你的任务是逐条对抗验证：先假设每条问题不成立，再到原文中找证据——"
    "是否误读了逻辑？是否有外层兜底使其无害？是否「看似 Bug 实为设计」？"
    "严重度是否被夸大？推翻一条不成立的问题与确认一条成立的问题同等重要；"
    "但把真问题说成误报是不可接受的错误——没有把握时给 partial 并写明疑点。"
    "用与用户一致的语言写作。"
)

ADVISOR_SYSTEM = (
    "你是一场对抗性审查中的建议者（修复方）。你拿到的问题清单已经过"
    "进攻方穷举与防守方复核，全部成立或部分成立。逐条给出可直接执行的"
    "修复方案：具体到代码级/步骤级的改法，而不是「建议优化」之类的空话；"
    "同时评估修复本身引入新问题的风险与优先级。用与用户一致的语言写作。"
)

JUDGE_SYSTEM = (
    "你是一场对抗性审查的终审裁判。你拿到的问题清单已经过进攻方穷举与"
    "防守方复核，你的任务是过滤噪音、按实际影响排序，产出一份精简、可"
    "执行的最终审查报告：删掉不值得处理的问题是你的职责，遗漏致命问题是"
    "你最大的失职。报告直接面向用户，不要提及内部角色与流程；用与用户"
    "一致的语言写作。"
)

# ---- 阶段用户消息模板（被审目标 + 阶段材料 + JSON 输出契约） ----

FINDER_USER_TEMPLATE = """【待审查内容】
{target}

【任务】
对以上内容做穷尽式问题扫描。扫描维度（不限于此）：
- 逻辑 Bug：边界条件、空值、竞态、错误分支
- 安全：注入、越权、敏感信息泄露
- 性能：复杂度、重复计算、资源泄漏
- 健壮性：异常处理缺失、降级缺失
- 设计：职责混乱、耦合、与上下文矛盾处

只输出一个 JSON 对象（不要任何解释文字），格式：
{{
  "findings": [
    {{
      "category": "bug|security|performance|robustness|design",
      "severity": "critical|high|medium|low",
      "location": "位置（文件:行号 / 函数名 / 逻辑位置；粘贴内容给大致行内位置）",
      "description": "问题描述：具体、可独立验证",
      "evidence": "依据：引用原文或推理链"
    }}
  ]
}}

一条都没有把握时输出空数组；除此之外，尽你所能多报。"""

INVESTIGATOR_USER_TEMPLATE = """【待审查内容】
{target}

【候选问题清单】（发现者提出，编号待你逐条裁决）
{findings_block}

【任务】
逐条对抗验证（先假设不成立，再找证据），只输出一个 JSON 对象（不要任何
解释文字），逐条给出裁决、不要遗漏任何编号：
{{
  "verdicts": [
    {{
      "id": "F-001",
      "verdict": "confirmed|refuted|partial",
      "reason": "裁决依据（尽量引用原文）",
      "corrected_description": "仅 partial 时：修正后的问题描述",
      "corrected_severity": "仅 partial 时：修正后的严重度"
    }}
  ]
}}"""

ADVISOR_USER_TEMPLATE = """【待审查内容】
{target}

【已确认成立的问题】（经调查员复核）
{findings_block}

【任务】
逐条给出修复方案，只输出一个 JSON 对象（不要任何解释文字），逐条对应、
不要遗漏任何编号：
{{
  "solutions": [
    {{
      "id": "F-001",
      "fix": "具体修复方案（代码级/步骤级）",
      "risk": "低|中|高（修复引入新问题的风险）",
      "priority": "P0（立即）|P1（本迭代）|P2（后续）"
    }}
  ]
}}"""

JUDGE_USER_TEMPLATE = """【待审查内容】
{target}

【对抗审查中间结果】
发现者提出 {total} 条候选问题；调查员裁决：成立 {confirmed} 条、部分成立
{partial} 条、推翻 {refuted} 条{pending_note}。

{findings_block}

【任务】
产出最终审查报告（Markdown）：
1. 只保留真正值得处理的问题——成立但影响极小的可以降级或剔除；
2. 按实际影响排序，每条给出：位置、问题、修复方案（有方案用方案，没有就给方向）；
3. 被推翻的典型误报可简要点评（帮助读者理解边界，不必逐条罗列）；
4. 结尾给一段总体结论。

报告直接面向用户，不要提及「发现者/调查员/建议者」等内部角色与流程。"""

JSON_FIX_INSTRUCTION = (
    "上一次回复无法解析为要求的 JSON（或缺少必需字段）。请重新输出："
    "只输出一个完整的 JSON 对象，严格符合此前给出的格式，不要包含任何"
    "解释文字。"
)

# 发现者空结果的兜底报告（不调裁判模型：高召回设定下的空结果本身就是结论，
# 如实标注来源，省一次调用）
EMPTY_RESULT_REPORT = (
    "## 对抗审查报告\n\n"
    "发现者按高召回标准完成了穷尽式扫描，未提出任何候选问题。\n\n"
    "（说明：本模式的发现者被明确鼓励过度报告，空结果通常意味着内容整体"
    "是干净的；若仍怀疑有遗漏，可换一个模型服务重新发起对抗审查。）"
)

# 代码围栏记号（chr(96) = 反引号；源码字符串里不裸写围栏，避免转义纠缠）
_FENCE = chr(96) * 3
_FENCED_JSON_RE = re.compile(_FENCE + r"(?:json)?\s*(.*?)\s*" + _FENCE, re.DOTALL)


def _json_candidates(text: str):
    """按优先级给出 JSON 候选片段：围栏块 → 整段 → 首个平衡大括号块。"""
    for m in _FENCED_JSON_RE.finditer(text):
        yield m.group(1).strip()
    stripped = text.strip()
    yield stripped
    start = stripped.find("{")
    if start < 0:
        return
    depth = 0
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                yield stripped[start : i + 1]
                return


def extract_json_object(text: str) -> dict | None:
    """从模型回复中提取首个可解析的 JSON 对象；失败返回 None。"""
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


async def _notice_retry(emit: EmitFn, label: str, error: str, attempt: int) -> None:
    delay = min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), 10.0)
    await emit(NoticeEvent(
        message=f"对抗 {label} 调用失败（{error[:120]}），"
                f"{delay:.0f} 秒后自动重试（第 {attempt}/{MAX_STREAM_RETRIES} 次）…"
    ))
    await asyncio.sleep(delay)


async def _run_text_turn(
    provider: Provider,
    messages: list[Message],
    timeout_s: int,
    emit: EmitFn,
    *,
    label: str,
    stream_out: bool = False,
) -> tuple[str, str, int, int, int]:
    """一次纯文本模型调用：流式收集 + 瞬态重试 + 超时，失败隔离在自身。

    stream_out=True 时把文本增量作为 TextDelta 转发（裁判阶段用：最终
    报告直接流进主对话，与圆桌融合文本同姿态）。返回
    (text, error, input_tokens, output_tokens, cached_tokens)；
    error="cancelled" 表示用户取消，已产出文本照常返回（同圆桌 _run_fusion）。
    """
    parts: list[str] = []
    usage_in = usage_out = usage_cached = 0

    async def consume() -> None:
        nonlocal usage_in, usage_out, usage_cached
        async for pe in provider.stream(
            messages, [], effort=resolve_auto_effort(provider, messages),
        ):
            if isinstance(pe, ProviderTextDelta):
                parts.append(pe.text)
                if stream_out:
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
                error = f"{label}超时（>{timeout_s} 秒）"
                retryable = not parts and attempt <= MAX_STREAM_RETRIES
            except Exception as e:  # noqa: BLE001 - 任何异常都归入本次调用失败，不上抛
                error = str(e)[:200]
                retryable = (
                    not parts and attempt <= MAX_STREAM_RETRIES and is_transient_error(e)
                )
            if retryable:
                await _notice_retry(emit, label, error, attempt)
                continue
            break  # 超时 / 非瞬态错误 / 已有内容：停止重试
    except asyncio.CancelledError:
        # 用户取消：已流出的部分文本交给调用方保留（同圆桌融合取消语义）
        asyncio.current_task().uncancel()
        return "".join(parts), "cancelled", usage_in, usage_out, usage_cached

    return "".join(parts), error, usage_in, usage_out, usage_cached


async def _call_json(
    provider: Provider,
    messages: list[Message],
    timeout_s: int,
    emit: EmitFn,
    *,
    label: str,
    expect_key: str,
) -> tuple[dict | None, str, int, int, int]:
    """一次要求 JSON 输出的调用：解析失败追加一次格式纠错重试。

    重试把上次的原始回复与纠错指令回喂（assistant + user 追加），让模型
    自己修格式——LLM 输出 JSON 的失败大多是围栏/前后缀，一次纠错命中率
    极高；再失败就如实返回错误，由阶段层降级。expect_key 用于确认顶层
    结构对路（防止解析出一个不相干的对象）。
    """
    text, error, u_in, u_out, u_cached = await _run_text_turn(
        provider, messages, timeout_s, emit, label=label,
    )
    total_in, total_out, total_cached = u_in, u_out, u_cached
    if error:
        return None, error, total_in, total_out, total_cached

    data = extract_json_object(text)
    if data is not None and expect_key in data:
        return data, "", total_in, total_out, total_cached

    fixed_text, error2, i2, o2, c2 = await _run_text_turn(
        provider,
        [*messages,
         Message.assistant([TextBlock(text=text or "(空回复)")]),
         Message.user(JSON_FIX_INSTRUCTION)],
        timeout_s, emit, label=f"{label}格式纠错",
    )
    total_in += i2
    total_out += o2
    total_cached += c2
    if error2:
        return None, error2, total_in, total_out, total_cached
    data = extract_json_object(fixed_text)
    if data is not None and expect_key in data:
        return data, "", total_in, total_out, total_cached
    return None, f"{label}未产出可解析的 JSON", total_in, total_out, total_cached


@dataclass
class Finding:
    """一条候选问题：发现者提出，调查员裁决，建议者补方案。"""

    finding_id: str
    category: str = "unknown"
    severity: str = "medium"
    location: str = ""
    description: str = ""
    evidence: str = ""
    verdict: str = VERDICT_PENDING
    verdict_reason: str = ""
    # partial 修正（调查员给出后优先生效）
    corrected_description: str = ""
    corrected_severity: str = ""
    # 建议者产出
    solution: str = ""
    risk: str = ""
    priority: str = ""

    @property
    def effective_description(self) -> str:
        return self.corrected_description or self.description

    @property
    def effective_severity(self) -> str:
        return self.corrected_severity or self.severity


@dataclass
class AdversarialOutcome:
    """整场对抗的结果：问题清单（含裁决与方案）+ 裁判报告与用量。

    status：
    - done：正常走完（裁判报告已产出；个别阶段降级不影响整体 done）
    - error：发现者失败（没有问题清单，后续阶段无从谈起）或裁判失败
    - cancelled：用户取消（已裁决结论与部分报告文本保留）
    """

    findings: list[Finding] = field(default_factory=list)
    report: str = ""
    status: str = "done"
    error: str = ""
    # 各角色用量逐条记录（含格式纠错重试的累计），形状与 usage_rows 输出
    # 一致，供 backend 写 usage_log
    role_usage: list[dict] = field(default_factory=list)


def usage_rows(outcome: AdversarialOutcome) -> list[dict]:
    """按角色逐条的用量记录（供 usage_log 入账；零用量行剔除）。"""
    return [
        {
            "provider": r["provider"],
            "model": r["model"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cached_tokens": r.get("cached_tokens", 0),
        }
        for r in outcome.role_usage
        if r.get("input_tokens") or r.get("output_tokens")
    ]


def stats(outcome: AdversarialOutcome) -> dict:
    """裁决统计（对抗卡与 finished 摘要用）。"""
    counts = {
        VERDICT_CONFIRMED: 0, VERDICT_REFUTED: 0,
        VERDICT_PARTIAL: 0, VERDICT_PENDING: 0,
    }
    for f in outcome.findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    return {"total": len(outcome.findings), **counts}


def findings_meta(outcome: AdversarialOutcome) -> list[dict]:
    """findings 的持久化形状（随 assistant 消息 meta 落库，对抗卡展开回看用）。"""
    return [
        {
            "id": f.finding_id,
            "category": f.category,
            "severity": f.effective_severity,
            "location": f.location,
            "description": clip_draft(f.effective_description.strip()),
            "verdict": f.verdict,
            "reason": clip_draft(f.verdict_reason.strip()),
            "solution": clip_draft(f.solution.strip()),
        }
        for f in outcome.findings
    ]


def _findings_block(findings: list[Finding]) -> str:
    """调查/建议阶段的问题清单分节（编号是 JSON 回传的对齐键，不可乱序）。"""
    lines = []
    for f in findings:
        lines.append(
            f"- {f.finding_id} [{f.effective_severity}] {f.category}"
            f" @ {f.location or '未标注'}\n"
            f"  描述：{f.effective_description}\n"
            f"  依据：{f.evidence or '（无）'}"
        )
    return "\n".join(lines)


_VERDICT_LABELS = {
    VERDICT_CONFIRMED: "成立",
    VERDICT_REFUTED: "已推翻",
    VERDICT_PARTIAL: "部分成立",
    VERDICT_PENDING: "待定（调查未完成）",
}


def _verdicted_block(findings: list[Finding]) -> str:
    """裁判阶段的中间结果分节：全部问题（含裁决与方案）。"""
    lines = []
    for f in findings:
        line = (
            f"- {f.finding_id} [{f.effective_severity}] {f.category}"
            f" @ {f.location or '未标注'}"
            f"（{_VERDICT_LABELS.get(f.verdict, f.verdict)}）\n"
            f"  描述：{f.effective_description}\n"
            f"  裁决依据：{f.verdict_reason or '（无）'}"
        )
        if f.solution:
            line += (
                f"\n  修复方案：{f.solution}"
                f"（风险：{f.risk or '未评'}；优先级：{f.priority or '未评'}）"
            )
        lines.append(line)
    return "\n".join(lines)


def _parse_findings(data: dict) -> list[Finding]:
    """发现者 JSON → Finding 列表（字段清洗：未知严重度/类别落默认值）。"""
    raw = data.get("findings")
    if not isinstance(raw, list):
        return []
    items = [
        it for it in raw
        if isinstance(it, dict) and str(it.get("description", "")).strip()
    ]
    findings: list[Finding] = []
    for i, item in enumerate(items):
        severity = str(item.get("severity", "")).strip().lower()
        findings.append(Finding(
            finding_id=f"F-{i + 1:03d}",
            category=str(item.get("category", "")).strip().lower() or "unknown",
            severity=severity if severity in _VALID_SEVERITIES else "medium",
            location=str(item.get("location", "")).strip(),
            description=str(item.get("description", "")).strip(),
            evidence=str(item.get("evidence", "")).strip(),
        ))
    return findings


def _apply_verdicts(findings: list[Finding], data: dict) -> None:
    """调查员 JSON → 就地写回各 Finding 的裁决（按 id 对齐；未知 id 忽略）。"""
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return
    by_id = {f.finding_id: f for f in findings}
    for item in raw:
        if not isinstance(item, dict):
            continue
        f = by_id.get(str(item.get("id", "")).strip())
        if f is None:
            continue
        verdict = str(item.get("verdict", "")).strip().lower()
        if verdict not in _VALID_VERDICTS:
            continue  # 无法识别的裁决保持 pending
        f.verdict = verdict
        f.verdict_reason = str(item.get("reason", "")).strip()
        if verdict == VERDICT_PARTIAL:
            f.corrected_description = str(item.get("corrected_description", "")).strip()
            corrected_severity = str(item.get("corrected_severity", "")).strip().lower()
            if corrected_severity in _VALID_SEVERITIES:
                f.corrected_severity = corrected_severity


def _apply_solutions(findings: list[Finding], data: dict) -> None:
    """建议者 JSON → 就地写回各 Finding 的方案（按 id 对齐；未知 id 忽略）。"""
    raw = data.get("solutions")
    if not isinstance(raw, list):
        return
    by_id = {f.finding_id: f for f in findings}
    for item in raw:
        if not isinstance(item, dict):
            continue
        f = by_id.get(str(item.get("id", "")).strip())
        if f is None:
            continue
        f.solution = str(item.get("fix", "")).strip()
        f.risk = str(item.get("risk", "")).strip()[:20]
        f.priority = str(item.get("priority", "")).strip()[:8]


def _phase_messages(system_text: str, history: list[Message], user_payload: str) -> list[Message]:
    """阶段消息：角色系统提示词 + 会话上下文 + 本阶段材料。

    图片块由调用方剥除（同圆桌）；四个阶段的消息互不共享——上下文隔离
    是对抗有效性的前提（见模块 docstring）。
    """
    msgs: list[Message] = [Message.system(system_text)]
    msgs.extend(dialogue(history))
    msgs.append(Message.user(user_payload))
    return msgs


async def run_adversarial(
    *,
    finder: MemberSpec,
    investigator: MemberSpec,
    advisor: MemberSpec | None,
    judge: Provider,
    judge_provider: str = "",
    judge_model: str = "",
    history: list[Message],
    user_text: str,
    timeout_s: int,
    emit: EmitFn,
    max_findings: int = 20,
) -> AdversarialOutcome:
    """跑一整场对抗：发现 → 调查 → 建议 → 裁判。

    - finder/investigator/advisor：MemberSpec（构建失败 provider=None 时该
      角色按失败隔离降级；advisor 允 None = 显式跳过建议阶段）；
    - judge：裁判 Provider（缺省由 backend 传当前主模型，同圆桌主席）；
    - history：会话上下文（图片块须由调用方剥除，同圆桌）；user_text 是
      被审目标（本轮用户消息原文）；
    - 返回完整结果，不直接改写主历史；取消时 status=cancelled，已产出
      内容（裁决、方案、部分报告）保留。
    """
    outcome = AdversarialOutcome()
    target = clip_draft(user_text.strip())

    def _usage_row(role: str, spec: MemberSpec | None, i: int, o: int, c: int,
                   provider: str = "", model: str = "") -> None:
        if i or o:
            outcome.role_usage.append({
                "role": role,
                "provider": provider or (spec.provider_name if spec else ""),
                "model": model or (spec.model if spec else ""),
                "input_tokens": i, "output_tokens": o, "cached_tokens": c,
            })

    async def _finish(status: str, summary: str) -> AdversarialOutcome:
        outcome.status = status
        await emit(AdversarialFinished(status=status, summary=summary))
        return outcome

    await emit(AdversarialStarted(roles=[
        {"role": PHASE_FINDER,
         "provider": finder.provider_name, "model": finder.model},
        {"role": PHASE_INVESTIGATOR,
         "provider": investigator.provider_name, "model": investigator.model},
        {"role": PHASE_ADVISOR,
         "provider": advisor.provider_name if advisor else "",
         "model": advisor.model if advisor else ""},
        {"role": PHASE_JUDGE, "provider": judge_provider, "model": judge_model},
    ]))

    try:
        # ---- 阶段一：发现者（穷尽式扫描） ----
        await emit(AdversarialPhase(phase=PHASE_FINDER))
        if finder.provider is None:
            outcome.error = f"发现者不可用：{finder.build_error or '模型服务不可用'}"
            await emit(NoticeEvent(message=outcome.error))
            return await _finish("error", outcome.error)

        data, error, u_in, u_out, u_cached = await _call_json(
            finder.provider,
            _phase_messages(FINDER_SYSTEM, history,
                            FINDER_USER_TEMPLATE.format(target=target)),
            timeout_s, emit, label="发现者", expect_key="findings",
        )
        _usage_row(PHASE_FINDER, finder, u_in, u_out, u_cached)
        if error == "cancelled":
            return await _finish("cancelled", "用户中止对抗审查（发现阶段）")
        if data is None:
            outcome.error = f"发现阶段失败：{error}"
            await emit(NoticeEvent(message=outcome.error + "；本场对抗无法继续。"))
            return await _finish("error", outcome.error)

        findings = _parse_findings(data)
        if len(findings) > max_findings:
            await emit(NoticeEvent(
                message=f"发现者提出 {len(findings)} 条候选问题，超出单场上限"
                        f"（{max_findings}，设置 · 对抗 可调），仅保留前 {max_findings} 条。"
            ))
            findings = findings[:max_findings]
        outcome.findings = findings
        for f in findings:
            await emit(AdversarialFindingProposed(
                finding_id=f.finding_id, category=f.category, severity=f.severity,
                location=f.location, description=clip_draft(f.description),
            ))

        if not findings:
            # 高召回设定下的空结果：省掉后续三次调用，兜底报告如实标注来源
            await emit(AdversarialPhase(phase=PHASE_JUDGE, note="空结果短路"))
            await emit(TextDelta(text=EMPTY_RESULT_REPORT))
            outcome.report = EMPTY_RESULT_REPORT
            return await _finish("done", "发现者未提出任何候选问题")

        # ---- 阶段二：调查员（对抗验证） ----
        await emit(AdversarialPhase(
            phase=PHASE_INVESTIGATOR, note=f"{len(findings)} 条候选问题待验证",
        ))
        if investigator.provider is None:
            await emit(NoticeEvent(
                message=f"调查者不可用：{investigator.build_error or '模型服务不可用'}；"
                        f"{len(findings)} 条问题保持待定，交裁判一并呈现。"
            ))
        else:
            data, error, u_in, u_out, u_cached = await _call_json(
                investigator.provider,
                _phase_messages(INVESTIGATOR_SYSTEM, history,
                                INVESTIGATOR_USER_TEMPLATE.format(
                                    target=target,
                                    findings_block=_findings_block(findings))),
                timeout_s, emit, label="调查者", expect_key="verdicts",
            )
            _usage_row(PHASE_INVESTIGATOR, investigator, u_in, u_out, u_cached)
            if error == "cancelled":
                return await _finish("cancelled", "用户中止对抗审查（调查阶段）")
            if data is None:
                await emit(NoticeEvent(
                    message=f"调查阶段失败：{error}；全部问题保持待定，交裁判一并呈现。"
                ))
            else:
                _apply_verdicts(findings, data)
        for f in findings:
            if f.verdict == VERDICT_PENDING:
                continue  # 没拿到裁决的不发裁决事件（终态 meta 里如实呈现）
            await emit(AdversarialVerdict(
                finding_id=f.finding_id, verdict=f.verdict,
                reason=clip_draft(f.verdict_reason),
            ))

        # ---- 阶段三：建议者（只处理成立/部分成立） ----
        to_solve = [
            f for f in findings
            if f.verdict in (VERDICT_CONFIRMED, VERDICT_PARTIAL)
        ]
        if to_solve:
            if advisor is None or advisor.provider is None:
                await emit(NoticeEvent(
                    message="建议者未配置或不可用：跳过修复方案阶段，"
                            "裁决与终审报告照常。"
                ))
            else:
                await emit(AdversarialPhase(
                    phase=PHASE_ADVISOR, note=f"{len(to_solve)} 条成立问题待给方案",
                ))
                data, error, u_in, u_out, u_cached = await _call_json(
                    advisor.provider,
                    _phase_messages(ADVISOR_SYSTEM, history,
                                    ADVISOR_USER_TEMPLATE.format(
                                        target=target,
                                        findings_block=_findings_block(to_solve))),
                    timeout_s, emit, label="建议者", expect_key="solutions",
                )
                _usage_row(PHASE_ADVISOR, advisor, u_in, u_out, u_cached)
                if error == "cancelled":
                    return await _finish("cancelled", "用户中止对抗审查（建议阶段）")
                if data is None:
                    await emit(NoticeEvent(
                        message=f"建议阶段失败：{error}；修复方案留空，终审报告照常。"
                    ))
                else:
                    _apply_solutions(to_solve, data)

        # ---- 阶段四：裁判（报告流式进主对话，同圆桌融合） ----
        s = stats(outcome)
        await emit(AdversarialPhase(phase=PHASE_JUDGE, note="终审裁决"))
        judge_payload = JUDGE_USER_TEMPLATE.format(
            target=target,
            total=s["total"],
            confirmed=s[VERDICT_CONFIRMED],
            partial=s[VERDICT_PARTIAL],
            refuted=s[VERDICT_REFUTED],
            pending_note=(
                f"、待定 {s[VERDICT_PENDING]} 条" if s[VERDICT_PENDING] else ""
            ),
            findings_block=_verdicted_block(findings),
        )
        report, error, u_in, u_out, u_cached = await _run_text_turn(
            judge, _phase_messages(JUDGE_SYSTEM, history, judge_payload),
            max(timeout_s * 2, 300), emit, label="裁判", stream_out=True,
        )
        _usage_row(PHASE_JUDGE, None, u_in, u_out, u_cached,
                   provider=judge_provider, model=judge_model)
        outcome.report = report
        if error == "cancelled":
            return await _finish(
                "cancelled", "用户中止对抗审查（裁判阶段；部分报告已保留）",
            )
        if error:
            outcome.error = f"裁判阶段失败：{error}"
            await emit(NoticeEvent(message=outcome.error))
            return await _finish("error", outcome.error)

        summary = (
            f"发现 {s['total']} 条候选：成立 {s[VERDICT_CONFIRMED]}、"
            f"部分成立 {s[VERDICT_PARTIAL]}、推翻 {s[VERDICT_REFUTED]}、"
            f"待定 {s[VERDICT_PENDING]}"
        )
        return await _finish("done", summary)

    except asyncio.CancelledError:
        # 阶段间隙的取消（_run_text_turn 内部已各自收敛）；已产出内容保留
        asyncio.current_task().uncancel()
        return await _finish("cancelled", "用户中止对抗审查")
