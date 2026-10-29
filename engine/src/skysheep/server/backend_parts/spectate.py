"""远程观战（T4.1/T4.2）：只读摘要的构造与渠道推送。

定位见 docs/远程观战设计.md：纯呈现——摘要全部来自工厂/团队的只读快照，
不含对话正文、任务 prompt、预授权名单、绝对路径与任何密钥；两个 WS 方法
都 local_only，推送复用 cron 收尾同款的渠道出站小件（未配置静默、失败只
记日志），不给无人值守开任何新的写通道。
"""

from __future__ import annotations

import time
from typing import Any

from .automation import push_text_to_targets

_ATTENTION_LIMIT = 5


def compose_spectate_digest(
    state: dict, teams: list[dict], *, now: float | None = None
) -> str:
    """把工厂/团队的只读快照拼成给渠道的中文摘要（纯函数，方便单测）。

    state 是 ``factory_state()`` 的返回；teams 是各队 ``snapshot()["team"]``
    的列表。字段全部 ``.get`` 容错：快照缺键只少一行，不抛错。
    """
    now = time.time() if now is None else now
    counters = state.get("counters") or {}
    parts: list[str] = [
        "【SkySheep 观战快照】" + time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
        (
            f"工厂：在跑 {int(counters.get('running', 0))} · 排队 "
            f"{int(counters.get('queued', 0))} · 今日完成 "
            f"{int(counters.get('done_today', 0))} · 需留意 "
            f"{int(state.get('attention_total', len(state.get('attention') or [])))}"
        ),
    ]
    proj_bits = [
        f"{p.get('name') or p.get('id')} {len(p.get('lines') or [])} 条"
        for p in state.get("projects") or []
        if isinstance(p, dict)
    ]
    if proj_bits:
        parts.append("产线：" + "，".join(proj_bits))
    for item in (state.get("attention") or [])[:_ATTENTION_LIMIT]:
        if isinstance(item, dict):
            parts.append(f"需留意：{item.get('text') or ''}".rstrip("："))
    resident = 0
    if teams:
        parts.append("团队：")
        for team in teams:
            if not isinstance(team, dict):
                continue
            if team.get("resident"):
                resident += 1
            bits = [f"成员 {len(team.get('members') or [])}"]
            if team.get("tasks") is not None:
                bits.append(f"工单 {len(team.get('tasks') or [])} 张")
            name = team.get("name") or team.get("team_id") or "未命名队伍"
            parts.append(f"- {name}（{' · '.join(bits)}）")
    if resident:
        parts.append(f"其中常驻 {resident} 支")
    return "\n".join(parts)


class SpectateMixin:
    """远程观战：只读摘要与渠道推送（两方法都 local_only，注册见 _dispatch_automation）。"""

    async def spectate_digest(self) -> dict:
        state: dict[str, Any] = await self.factory_state()
        teams: list[dict] = []
        for team in (getattr(self, "_teams", {}) or {}).values():
            try:
                snap = team.snapshot()
            except Exception:  # noqa: BLE001 - 单队快照失败不拖垮整份摘要
                continue
            info = (snap or {}).get("team")
            if isinstance(info, dict):
                teams.append(info)
        digest = compose_spectate_digest(state, teams)
        return {
            "digest": digest,
            "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
        }

    async def spectate_send(self) -> dict:
        digest = (await self.spectate_digest())["digest"]
        delivered = await push_text_to_targets(
            getattr(self, "channels", None), digest, what="远程观战"
        )
        return {"delivered": delivered}
