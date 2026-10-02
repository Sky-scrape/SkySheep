"""每日 token 预算告警的渠道推送：用量越过 80% / 100% 档时各推一条中文提醒。

与「预算护栏」的分工：护栏（backend.send 开头的超限拒绝）是硬闸，到 100%
拦下新消息；告警是软提醒——80% 档提前打招呼、100% 档解释为什么被拦。
**前提：告警跟着渠道走**——没有启用且配置齐全的推送目标（聊天渠道 /
Webhook）就等于不告警，护栏照常在本应用内生效（见 README 功能列表）。

防重发状态存引擎自有状态文件 ``~/.skysheep/budget-alert-state.json``
（``textio.write_text_atomic`` 原子写），不走 config.toml：只有「日期 +
已发档位」一个防重发键，进配置文件反而要为它开设置段。同档每天最多一条
（推送失败也只记日志不重试，防给不可达端点刷一整天请求）；跨天自动重置
（日期对不上就当没发过）。状态损坏（坏 JSON / 形状不对）按没发过重建。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ...config import skysheep_home
from ...textio import write_text_atomic

STATE_FILE = "budget-alert-state.json"

# 告警档位（百分比）：80% 提前打招呼，100% 配合护栏拦截做解释
TIERS = (80, 100)


def state_path() -> Path:
    return skysheep_home() / STATE_FILE


def default_state() -> dict:
    return {"date": "", "sent": []}


def today_str(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))


def load_state(today: str | None = None) -> dict:
    """读防重发状态；缺文件 / 坏 JSON / 形状不对都按没发过重建。

    给出 ``today`` 时做跨天重置：日期对不上的一律视为空档（昨天的已发
    档位今天不作数）。``sent`` 只认真整数且必须是已知档位，其余丢弃。
    """
    state = default_state()
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    state["date"] = str(raw.get("date") or "")
    sent = raw.get("sent")
    if isinstance(sent, list):
        state["sent"] = sorted({t for t in sent if isinstance(t, int) and t in TIERS})
    if today is not None and state["date"] != today:
        state = default_state()
    return state


def save_state(state: dict) -> None:
    """原子写防重发状态（引擎自有状态文件一律原子写，见 textio.write_text_atomic）。

    未知字段不落盘：写出去的形状永远是自己读得回来的那两个键。
    """
    clean = default_state()
    clean["date"] = str(state.get("date") or "")
    sent = state.get("sent")
    if isinstance(sent, list):
        clean["sent"] = sorted({t for t in sent if isinstance(t, int) and t in TIERS})
    write_text_atomic(state_path(), json.dumps(clean, ensure_ascii=False, indent=2) + "\n")


def crossed_tiers(used: int, budget: int) -> list[int]:
    """当前用量已越过的档位（>= 档位线即算越过，含恰好压线）。"""
    if budget <= 0:
        return []
    return [t for t in TIERS if used >= budget * t // 100]


def due_tiers(used: int, budget: int, sent_today: list[int]) -> list[int]:
    """该推的档位 = 已越过 − 今天已发过（跨天重置由 load_state(today) 负责）。"""
    done = set(sent_today)
    return [t for t in crossed_tiers(used, budget) if t not in done]


def alert_body(tier: int, used: int, budget: int) -> str:
    """告警文案：今日用量 / 预算与百分比 / 建议（口径对齐护栏拦截提示）。"""
    pct = round(used / budget * 100) if budget > 0 else 0
    if tier >= 100:
        head = "已达上限"
        advice = (
            "已到上限，继续发消息会被每日预算护栏拦下；如需继续，"
            "请在 设置 · 高级 里调高或关闭「每日 token 预算」（明天自动重置）。"
        )
    else:
        head = f"已达 {tier}%"
        advice = (
            "用量接近上限，注意节奏；如需调整，"
            "请在 设置 · 高级 里修改或关闭「每日 token 预算」（明天自动重置）。"
        )
    return "\n".join([
        f"🐑 SkySheep 预算提醒：今日 token 用量{head}",
        f"今日用量：约 {used:,} tokens",
        f"每日预算：{budget:,} tokens（已用 {pct}%）",
        f"建议：{advice}",
    ])
