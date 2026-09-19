"""任务耗时预估：接手任务时给出一个预计完成时间区间（core/estimate.py）。

与思考强度「自动」档（effort.py）同一套思路：纯启发式、零额外请求。
信号来自三处——任务文本本身（寒暄 / 轻量 / 复杂信号 / 代码 / 步骤数 /
范围放大词）、近期历史（多步工具作业进行中）、本项目近期实测耗时（store
查询的中位数作先验校准，启发式再准也不如用户自己机器上的真实记录）。

产出秒级区间 [min, max]，随 TaskEstimate 事件下发；前端显示
「预计 X~Y 分钟」并在运行中对照已用时，超时给出弱提示。预估是参考值
不是承诺：依据说明（basis）随事件附带，悬停可见。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from ..messages import Message, ToolResultBlock, ToolUseBlock

# ---- 文本信号（与 effort.py 同族的词表，按「耗时」维度另配） ----

# 寒暄/应答类：整条消息就是这些词 → 秒回
_TRIVIAL_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|hey|在吗|在么|谢谢|多谢|感谢|辛苦了|麻烦了|"
    r"再见|拜拜|晚安|早安|好的|收到|明白|了解|ok|okay|继续|接着来|go on)[!！。~,，.~～\s]*$",
    re.IGNORECASE,
)

# 轻量任务：一问一答就能交付
_SIMPLE_TASK_RE = re.compile(
    r"翻译|润色|校对|错别字|错字|改写|缩写|是什么|是什么意思|什么叫|啥意思|"
    r"多少|几点|查一下|看一下|看看|解释|名词|定义"
)

# 复杂任务信号：多文件作业 / 排查 / 设计 / 批处理
_COMPLEX_RE = re.compile(
    r"调试|排错|排查|定位问题|报错|异常|崩溃|卡死|修复|修一下|bug|error|exception|"
    r"traceback|重构|迁移|架构|设计方案|方案设计|系统集成|数据流|状态机|"
    r"爬虫|抓取|批量|脚本|自动化|部署|打包|安装|配置环境|数据库|前后端|"
    r"完整实现|从零|从头|整套|一套|平台|系统|工具|网站|网页|界面|测试用例|单元测试"
)

# 范围放大词：命中越多活越大
_AMPLIFIER_RE = re.compile(r"完整|全部|所有|每一个|逐个|系统性|端到端|整个|全部文件|尽可能|尽量")

# 代码特征：围栏代码块或常见代码片段（带代码的任务通常要写文件、跑命令）
_CODE_RE = re.compile(
    r"```|def\s+\w+|class\s+\w+|function\s+\w+|import\s+\w+|const\s+\w+|let\s+\w+|"
    r"#include|void\s+\w+\(|SELECT\s+\S+\s+FROM|pip\s+install|npm\s+(i|install|run)"
)

# 步骤化任务：编号 / 项目符号行，或「首先…然后…最后」连接词
_STEP_LINE_RE = re.compile(r"(?m)^\s*(?:\d{1,2}[.、)）]\s*|[-*•·]\s+|[①②③④⑤⑥⑦⑧⑨⑩])")
_STEP_CONN_RE = re.compile(r"首先|其次|然后|接着|再然后|最后|第一步|第二步|第三步")

# 参与信号统计的近期历史条数（与 effort.py 口径一致）
_TAIL = 12
# 近期工具调用次数达到该值视为多步作业进行中
_MANY_TOOL_CALLS = 4

# 档位区间（秒）：score 越高预估越久，约 2 倍一档
# score = -2 及以下 → 秒回；>= 5 → 大工程
_RANGES: list[tuple[int, int]] = [
    (8, 30),      # 寒暄、确认
    (20, 60),     # 轻量问答
    (45, 150),    # 常规单步任务（写个小函数、改一处）
    (90, 300),    # 稍复杂（多轮工具、带代码）
    (180, 600),   # 多步作业（排查、小功能整链路）
    (300, 1200),  # 重活（跨文件重构、爬虫、小工具全套）
    (600, 2400),  # 大工程（从零实现、端到端系统）
]
_LEVELS = ["trivial", "light", "normal", "moderate", "heavy", "major", "major"]


@dataclass
class TaskEstimate:
    """一次任务的耗时预估结果（秒级区间 + 档位 + 人类可读依据）。"""

    min_seconds: int
    max_seconds: int
    level: str
    basis: str = ""


def _score_text(text: str) -> tuple[int, list[str]]:
    """按任务文本打分（0 为常规单步任务基准），返回 (score, 理由)。"""
    score = 0
    reasons: list[str] = []
    t = text.strip()
    if not t:
        return score, reasons
    if _TRIVIAL_RE.match(t):
        # 不提前返回：催促词（"继续"）撞上多步历史时，历史信号要把档位拉回来
        score -= 2
        reasons.append("一句话应答")
    if _SIMPLE_TASK_RE.search(t):
        score -= 1
        reasons.append("轻量问答")
    n_complex = len(set(_COMPLEX_RE.findall(t)))
    if n_complex == 1:
        score += 1
    elif n_complex >= 2:
        score += 2
        reasons.append("多道复杂信号")
    if _CODE_RE.search(t):
        score += 1
    if len(t) >= 400:
        score += 1
    if len(t) >= 1200:
        score += 1
    n_amp = len(set(_AMPLIFIER_RE.findall(t)))
    if n_amp:
        score += 1 if n_amp == 1 else 2
        reasons.append("范围要求大" if n_amp >= 2 else "带范围放大词")
    steps = len(_STEP_LINE_RE.findall(t))
    conns = len(set(_STEP_CONN_RE.findall(t)))
    if steps >= 6 or (steps >= 3 and conns >= 2):
        score += 2
        reasons.append(f"约 {steps} 个步骤")
    elif steps >= 3 or conns >= 2:
        score += 1
        reasons.append("分步任务")
    return score, reasons


def _score_history(messages: list[Message]) -> tuple[int, list[str]]:
    """近期历史里的作业信号：工具在跑 / 在报错 → 本轮多半是多步任务的一段。"""
    score = 0
    reasons: list[str] = []
    tail = [m for m in messages if m.role != "system"][-_TAIL:]
    for m in tail:
        for b in m.content:
            if isinstance(b, ToolResultBlock) and b.is_error:
                score += 1
                reasons.append("上一轮工具在报错")
                break
    tool_calls = sum(1 for m in tail for b in m.content if isinstance(b, ToolUseBlock))
    if tool_calls >= _MANY_TOOL_CALLS:
        score += 1
        reasons.append("多步作业进行中")
    return score, reasons


def _calibrate(lo: int, hi: int, recent: list[float] | None) -> tuple[int, int, list[str]]:
    """用本项目近期实测耗时校准启发式区间：中位数落在区间外就把边界拉过去。"""
    reasons: list[str] = []
    samples = [s for s in (recent or []) if 1 <= s <= 7200]
    if len(samples) < 3:
        return lo, hi, reasons
    med = statistics.median(samples)
    if med > hi:
        hi = min(int(med * 1.5), 4 * 3600)
        lo = max(lo, int(med * 0.4))
        reasons.append(f"参考最近 {len(samples)} 次实测（中位数 {_fmt_clock(med)}）")
    elif med < lo:
        lo = max(5, int(med * 0.8))
        reasons.append(f"实测比直觉快：最近 {len(samples)} 次中位数 {_fmt_clock(med)}")
    if lo >= hi:
        hi = lo * 2
    return lo, hi, reasons


def estimate_task(
    text: str,
    *,
    history: list[Message] | None = None,
    images: int = 0,
    members: int = 0,
    debate_rounds: int = 0,
    recent: list[float] | None = None,
) -> TaskEstimate:
    """按任务文本、近期历史与历史实测估一个完成时间区间（秒）。"""
    score, reasons = _score_text(text)
    if images:
        score += 1
        reasons.append("含图片输入")
    h_score, h_reasons = _score_history(history or [])
    score += h_score
    reasons.extend(h_reasons)
    if score > -2:
        # 历史信号把寒暄档拉了回来：依据里别再留自相矛盾的「一句话应答」
        reasons = [r for r in reasons if r != "一句话应答"]
    if score > 0:
        reasons = [r for r in reasons if r != "轻量问答"]

    idx = max(0, min(len(_RANGES) - 1, score + 2))
    lo, hi = _RANGES[idx]
    lo, hi, c_reasons = _calibrate(lo, hi, recent)
    reasons.extend(c_reasons)

    # 圆桌：多模型并行作答（耗时 ≈ 最慢成员 + 融合），按成员数温和放大
    if members > 1:
        factor = 1 + 0.35 * (members - 1) + (0.5 if debate_rounds >= 2 else 0)
        lo, hi = int(lo * factor), int(hi * factor)
        reasons.append(f"圆桌 {members} 家模型并行")
    return TaskEstimate(min_seconds=int(lo), max_seconds=int(hi),
                        level=_LEVELS[idx], basis=" · ".join(reasons))


def _fmt_clock(seconds: float) -> str:
    """单个时长的人类表述：45 秒 / 3 分 20 秒 / 1 小时 5 分。"""
    s = int(round(seconds))
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒" if sec else f"{m} 分钟"
    h, mm = divmod(m, 60)
    return f"{h} 小时 {mm} 分" if mm else f"{h} 小时"


def format_range(lo: int, hi: int) -> str:
    """区间表述用粗粒度单位：8~30 秒 / 3~8 分钟 / 1~2 小时。"""
    def unit(sec: float) -> int:
        # 半 upward 舍入（Python round 是银行家舍入，2.5 会舍到 2）
        return int(sec + 0.5)

    if hi < 60:
        return f"{lo} 秒" if lo == hi else f"{lo}~{hi} 秒"
    if hi < 3600:
        a, b = max(1, unit(lo / 60)), max(1, unit(hi / 60))
        return f"{a} 分钟" if a == b else f"{a}~{b} 分钟"
    a, b = max(1, unit(lo / 3600)), max(1, unit(hi / 3600))
    return f"{a} 小时" if a == b else f"{a}~{b} 小时"
