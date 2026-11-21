"""协作共享小件：圆桌 / 对抗等多模型协作链路共用的小工具。

为什么单独成模块：协作链路之间互不 import（同 collab_budget.py 的约定，
见其模块说明），把「都要用、又够不上各自领域模型」的小件收敛在这里——
本模块只依赖引擎基础件（events / models / agent 的重试常量），被圆桌
（roundtable.py）与对抗（adversarial.py）引用，但不引用它们任何一方。

目前收录：
- EmitFn：协作链路统一的事件回调形状（backend 负责转成 WS 帧广播）；
- MemberSpec：成员/角色身份（provider/model 标识 + 已构建 Provider），
  圆桌成员、对抗四角色与团队名册共用同一形状；
- clip_draft / DRAFT_CHAR_LIMIT / DRAFT_CLIP_MARK：单份草稿的截断小件，
  圆桌融合/辩论提示词与对抗的问题清单共用；
- notice_retry：瞬态错误重试的通知 + 指数退避等待（圆桌成员/融合与
  对抗各阶段共用，称谓文案参数化）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..events import AgentEvent, NoticeEvent
from ..models.base import Provider
from .agent import MAX_STREAM_RETRIES, RETRY_BASE_DELAY_S

# 发给前端的事件回调（backend 负责把它转成 WS 帧广播；圆桌/对抗各自签名里
# 的 emit 都用这一形状）
EmitFn = Callable[[AgentEvent], Awaitable[None]]


@dataclass
class MemberSpec:
    """一个协作成员/角色：provider/model 标识 + 已构建的 Provider 实例。

    圆桌成员、对抗的角色（发现者/调查员/建议者/裁判）共用同一形状。
    模板接入（1.9 协作模板）新增两个可选字段，全部默认空 = 旧行为逐字不变：
    - name：模板角色显示名（RoundtableStarted 与成员卡用）；
    - prompt：模板角色人设；非空时覆盖默认 MEMBER_SYSTEM（辩论轮拼在
      DEBATE_SYSTEM 之前，不整体替换）。
    """

    provider_name: str
    model: str
    provider: Provider | None = None  # 构建失败（如缺 Key）时为 None，作答时直接报错
    build_error: str = ""
    role: str = ""  # 身份预设 id（MEMBER_ROLES 键；空 = 普通成员）
    name: str = ""
    prompt: str = ""

    @property
    def template_prompt(self) -> str:
        """模板人设。"""
        return self.prompt.strip()


# 单份草稿进入融合/辩论/对抗提示词的字符上限：长草稿截断（带标记），
# 防止多成员长草稿把主席/成员自己的上下文撑爆。
DRAFT_CHAR_LIMIT = 12_000
DRAFT_CLIP_MARK = "\n…（草稿过长，已截断）"


def clip_draft(text: str) -> str:
    """草稿过长时截断（带省略标记），保护融合/辩论提示词不爆上下文。"""
    if len(text) <= DRAFT_CHAR_LIMIT:
        return text
    return text[:DRAFT_CHAR_LIMIT] + DRAFT_CLIP_MARK


async def notice_retry(
    emit: EmitFn, label: str, error: str, attempt: int, *,
    subject: str, base_delay_s: float = RETRY_BASE_DELAY_S,
) -> None:
    """瞬态错误重试的通知 + 指数退避等待（圆桌成员/融合与对抗阶段共用）。

    subject 是文案里 label 前的称谓（圆桌用「圆桌成员」、对抗用「对抗」）；
    其余文案与退避节奏保持抽出的两处原样：错误截 120 字符、base_delay_s 起
    指数翻倍封顶 10 秒、上限 MAX_STREAM_RETRIES 次。base_delay_s 由调用方
    显式传各自模块的 RETRY_BASE_DELAY_S——既有的「monkeypatch 模块常量加速
    测试」写法（test_roundtable 等）保持原样生效。
    """
    delay = min(base_delay_s * (2 ** (attempt - 1)), 10.0)
    await emit(NoticeEvent(
        message=f"{subject} {label} 调用失败（{error[:120]}），"
                f"{delay:.0f} 秒后自动重试（第 {attempt}/{MAX_STREAM_RETRIES} 次）…"
    ))
    await asyncio.sleep(delay)
