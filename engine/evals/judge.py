"""LLM-as-judge：用真实模型给回答打分（可选层，绝不进默认回归）。

指标层（``_metrics.py``）能回答「多贵、多慢、调了几次工具」，回答不了「这份
回答是否真的解决了问题」——后者要靠裁判模型。这里给一个最小、可脚本化的
裁判实现：

- 输入问题、回答、评分标准（自然语言要点），要求裁判只回一个 JSON：
  ``{"score": 0-10, "verdict": "pass"|"fail", "reasons": [...]}``；
- 解析**宽容**（围栏代码块、前后废话、中文引号都认），解析失败返回
  ``JudgeVerdict(parsed=False, ...)`` 而不是抛异常——裁判输出格式抖动不该
  让整场评测崩掉，调用方按 parsed=False 记为「无效样本」；
- 随机性由调用方控制（温度、种子按 provider 能力设），本模块不假装确定性。

**不进 CI / 默认回归**：需要真实 Key、有随机性与费用。用法是在自己的脚本里
显式调用 ``judge_answer``（写临时用例也可以，但别放进 ``tests/`` 与 ``evals/``
的默认收集——那会让每台机器都必须有 Key）。``tests/test_eval_metrics.py`` 用
FakeProvider 钉住解析与提示词契约，不产生真实调用。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field

from skysheep.messages import Message
from skysheep.models.base import Provider, ProviderDone, ProviderTextDelta

__all__ = [
    "JUDGE_SYSTEM",
    "COMPARE_SYSTEM",
    "CompareVerdict",
    "JudgeVerdict",
    "complete_text",
    "judge_answer",
    "judge_compare",
    "parse_compare",
]

JUDGE_SYSTEM = """你是严格的回答质量裁判。只依据给定的「问题」「评分标准」评「候选回答」，
不要因为措辞风格加分或扣分。按下面的 JSON 输出，且只输出 JSON：
{"score": <0 到 10 的整数>, "verdict": "pass" 或 "fail", "reasons": ["...", "..."]}
判定规则：
- 评分标准里的每个要点都满足才可 pass；
- 与问题无关、编造事实（标准里没有依据的断言）、答非所问都给 fail；
- 长度中性：不以「写得多」加分，漏了标准要点的长回答照样扣；
- 只依据「问题」与「评分标准」里给出的信息，不引入外部知识补标准；
- reasons 用中文，每条一句话，指出具体缺什么或哪里错（最多 4 条）。"""

_MAX_ANSWER_CHARS = 8000
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class JudgeVerdict:
    score: int = 0
    verdict: str = "fail"
    reasons: list[str] = field(default_factory=list)
    parsed: bool = False
    raw: str = ""

    @property
    def passed(self) -> bool:
        return self.parsed and self.verdict == "pass" and self.score >= 6


async def complete_text(provider: Provider, prompt: str, system: str = "") -> str:
    """驱动一次 provider 流式调用并拼出纯文本（judge 与评测脚本共用）。"""
    messages: list[Message] = []
    if system:
        messages.append(Message.system(system))
    messages.append(Message.user(prompt))
    parts: list[str] = []
    async for ev in provider.stream(messages, []):
        if isinstance(ev, ProviderTextDelta):
            parts.append(ev.text)
        elif isinstance(ev, ProviderDone):
            break
    return "".join(parts)


def parse_verdict(raw: str) -> JudgeVerdict:
    """从裁判原文里抠出 JSON 并归一化；抠不出来返回 parsed=False。"""
    text = (raw or "").strip()
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text)
    obj = _OBJ_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            score = int(round(float(data.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in ("pass", "fail"):
            verdict = "pass" if score >= 6 else "fail"
        reasons = data.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list):
            reasons = []
        return JudgeVerdict(
            score=max(0, min(10, score)),
            verdict=verdict,
            reasons=[str(r)[:300] for r in reasons][:4],
            parsed=True,
            raw=raw,
        )
    return JudgeVerdict(parsed=False, raw=raw)


async def judge_answer(
    provider: Provider, *, question: str, answer: str, criteria: str,
    max_answer_chars: int = _MAX_ANSWER_CHARS,
) -> JudgeVerdict:
    """用裁判模型给一次回答打分（一次 provider 调用，无重试）。"""
    clipped = (answer or "")[:max_answer_chars]
    prompt = (
        f"## 问题\n{question.strip()}\n\n"
        f"## 评分标准\n{criteria.strip()}\n\n"
        f"## 候选回答\n{clipped}\n\n"
        "只输出 JSON。"
    )
    raw = await complete_text(provider, prompt, JUDGE_SYSTEM)
    return parse_verdict(raw)


# ---------------------------------------------------------------- 对照评审

COMPARE_SYSTEM = """你是严格的回答质量裁判。给你一个问题、评分标准和两份候选回答（A 与 B），
只依据「问题」与「评分标准」判断哪份更好，不要因为更长、措辞更流畅或排在前面的
就偏向谁。按下面的 JSON 输出，且只输出 JSON：
{"winner": "A" 或 "B" 或 "tie", "reasons": ["...", "..."]}
判定规则：
- 两份都不满足标准、或满足程度确实相当 → "tie"；
- 编造事实（标准里没有依据的断言）比漏答更严重：一方编造即判另一方胜；
- reasons 用中文，每条一句话，说明胜者（或平局）的具体依据（最多 3 条）。"""


@dataclass
class CompareVerdict:
    """一次 A/B 对照评审的结果。

    winner 是规范化的（恒以传入的 answer_a / answer_b 视角表述，与展示顺序
    无关）；consistent=False 表示两次评审（换序复核） winner 不一致——位置
    偏差或输出抖动的信号，调用方应记「需人工复核」而不是强行采信。
    """

    winner: str = "tie"  # "a" / "b" / "tie"
    reasons: list[str] = field(default_factory=list)
    consistent: bool = False
    parsed: bool = False
    raw: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return not (self.parsed and self.consistent)


def parse_compare(raw: str) -> tuple[str, list[str], bool]:
    """从裁判原文抠出 {"winner": "A"|"B"|"tie"}；抠不出来返回 ("", [], False)。"""
    text = (raw or "").strip()
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text)
    obj = _OBJ_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        winner = str(data.get("winner") or "").strip().lower()
        if winner not in ("a", "b", "tie"):
            continue
        reasons = data.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list):
            reasons = []
        return winner, [str(r)[:300] for r in reasons][:3], True
    return "", [], False


async def judge_compare(
    provider: Provider, *, question: str, criteria: str,
    answer_a: str, answer_b: str,
    seed: int | None = None,
    max_answer_chars: int = _MAX_ANSWER_CHARS,
) -> CompareVerdict:
    """A/B 对照评审：评审两次（第二次换序），结论一致才算数。

    为什么两次：LLM 裁判有位置偏差（偏向前者/更长者的倾向，LLM-as-judge
    的已知问题）。同一对回答只评一次，结论里混着「谁真的好」与「谁恰好排
    在前面」；换序重评一次，两次 winner 一致才判 consistent——不一致时
    返回 needs_human=True，交给人工，不强行采信。

    seed 控制第一次的展示顺序（可复现；None 时随机）。答案截断到
    max_answer_chars 后进入提示词。
    """
    order = random.Random(seed).choice(("ab", "ba"))  # 非加密用途：评测展示顺序要可复现，弱随机正合适
    answers = {
        "A": (answer_a or "")[:max_answer_chars],
        "B": (answer_b or "")[:max_answer_chars],
    }

    def _prompt(label_first: str, label_second: str) -> str:
        return (
            f"## 问题\n{question.strip()}\n\n"
            f"## 评分标准\n{criteria.strip()}\n\n"
            f"## 候选回答 {label_first}\n{answers[label_first]}\n\n"
            f"## 候选回答 {label_second}\n{answers[label_second]}\n\n"
            "只输出 JSON。"
        )

    # 标签（A/B）跟着内容走：裁判按标签回答，标签到规范视角（a/b）是恒等映射；
    # 换的只是两块内容的展示先后——位置偏差因此被翻到反面，两次一致才算数。
    raws: list[str] = []
    winners: list[str] = []
    parsed_any = False
    for labels in ((order[0].upper(), order[1].upper()),
                   (order[1].upper(), order[0].upper())):
        raw = await complete_text(provider, _prompt(labels[0], labels[1]), COMPARE_SYSTEM)
        raws.append(raw)
        winner, _reasons, ok = parse_compare(raw)
        parsed_any = parsed_any or ok
        winners.append(winner if ok else "")
    consistent = bool(parsed_any and winners[0] and winners[0] == winners[1])
    # 结论取第一次评审的；reasons 也取第一次的原文
    winner, reasons, ok = parse_compare(raws[0])
    return CompareVerdict(
        winner=(winner if ok else "tie"),
        reasons=reasons,
        consistent=consistent,
        parsed=parsed_any,
        raw=raws,
    )
