"""接力配置公共类型与无工具运行期原语。

本模块只做纯数据与纯计算：状态常量、预算计算、交接包构造、以及「对单个
节点发一次无工具纯文本模型调用」这一动作（``provider.stream(messages, [])``）。

**绝不导入或调用 Agent / chat.send / hooks / MCP / 工具注册表**——接力运行期
不经这些路径；节点一旦请求工具即视为协议错误并暂停。数据定义保持独立于
core 的 Agent 导入链（供 relayconfig / store / backend 共用）。
"""
from __future__ import annotations

import asyncio
import hashlib
import time

from ..messages import Message
from ..models.base import (
    Provider,
    ProviderDone,
    ProviderTextDelta,
    ProviderToolUse,
)
from ..relayconfig import RelayConfig, RelayEdge, RelayNode, RelaySlot, RelayWorkflow

__all__ = [
    "RelayConfig",
    "RelayEdge",
    "RelayNode",
    "RelaySlot",
    "RelayWorkflow",
    "RELAY_STATES",
    "RELAY_TERMINAL_STATES",
    "RelayProtocolError",
    "RelayLimitError",
    "compute_budget",
    "report_hash",
    "ordered_node_ids",
    "next_node_id",
    "build_node_messages",
    "build_packet",
    "packet_to_text",
    "run_node_text",
]

# 运行期状态（小写持久化值）。interrupted 由启动恢复写入，仍需显式 resume。
RELAY_STATES: tuple[str, ...] = (
    "running",
    "waiting_confirmation",
    "paused",
    "stopping",
    "stopped",
    "completed",
    "interrupted",
)
RELAY_TERMINAL_STATES: tuple[str, ...] = ("stopped", "completed")


class RelayProtocolError(Exception):
    """节点协商期间出现工具调用（协议错误）：本流程无工具，必须暂停。"""


class RelayLimitError(Exception):
    """受控限制：阶段超时 / 输出超限 / 上下文余量不足 / 预算或步数越线。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def compute_budget(capacity: int, ratio: float, reserve: int, recent: int) -> tuple[int, int]:
    """由上下文容量 C 计算 (触发交接阈值 T, 最小预留 R)。

    R = max(配置 reserve, 1024)；T = min(floor(C*ratio), C-R)。容量不足以容纳
    最低可用预算时 T <= 0，调用方应在启动时拒绝而不是把负 T 当无限交接。
    """
    cap = int(capacity)
    r = max(int(reserve), 1024)
    t = min(int(cap * float(ratio)), cap - r)
    return t, r


def report_hash(text: str) -> str:
    """节点报告的内容哈希（sha256，hex）：批准时按它绑定具体方案版本。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def ordered_node_ids(workflow: RelayWorkflow) -> list[str]:
    """按起始节点沿串行连线展开的节点顺序（配置校验保证无环、全可达）。"""
    outgoing = {e.source: e.target for e in workflow.edges}
    out: list[str] = []
    current: str | None = workflow.start_node
    while current is not None and current not in out:
        out.append(current)
        current = outgoing.get(current)
    return out


def next_node_id(workflow: RelayWorkflow, node_id: str) -> str | None:
    for edge in workflow.edges:
        if edge.source == node_id:
            return edge.target
    return None


def build_node_messages(role: str, prompt: str, packet_text: str = "") -> list[Message]:
    """构造单节点的无工具纯文本请求（system 约束 + 交接材料 + 节点指令）。

    系统约束只声明「不得使用工具、只输出文本」这一协议事实，不授予任何权限。
    """
    system = (
        "你正在参与一次「接力」多模型串行协作，你是固定槽位角色："
        f"{role}。本流程为纯文本协商：你没有任何工具可用，"
        "绝不要请求、模拟或假称调用了任何工具；只输出文本。"
        "请严格按节点指令产出一份条理清晰的文字报告，"
        "并在结尾用一行「报告完毕」标记节点目标已完成。"
    )
    blocks: list[str] = []
    if packet_text:
        blocks.append("【交接材料】\n" + packet_text)
    blocks.append("【本节点指令】\n" + prompt)
    return [Message.system(system), Message.user("\n\n".join(blocks))]


def build_packet(
    *,
    task_id: str,
    goal: str,
    completed: list[dict],
    recent_messages: list[str] | None = None,
    budget: dict | None = None,
) -> dict:
    """交接包 v1（本地数据；不自动上传文件、不拉取外部 URL）。"""
    return {
        "schema_version": 1,
        "task_id": task_id,
        "created_at": time.time(),
        "goal": goal,
        "completed": completed,
        "recent_messages": recent_messages or [],
        "budget": budget or {},
    }


def packet_to_text(packet: dict) -> str:
    """把交接包渲染成注入模型的纯文本（不做任何 HTML / 模板解释）。"""
    if not packet:
        return ""
    lines = ["总目标：" + str(packet.get("goal", ""))]
    completed = packet.get("completed") or []
    if completed:
        lines.append("已完成节点报告（节选）：")
        for item in completed:
            lines.append(
                "- [{}/{}] {}：{}".format(
                    item.get("node_id", ""),
                    item.get("slot", ""),
                    item.get("report_id", ""),
                    item.get("summary", ""),
                )
            )
    recent = packet.get("recent_messages") or []
    if recent:
        lines.append("近期上下文：")
        lines.extend("- " + str(r) for r in recent)
    return "\n".join(lines)


async def run_node_text(
    provider: Provider,
    messages: list[Message],
    *,
    timeout_s: int,
    max_output_chars: int,
) -> dict:
    """对单个节点发一次**无工具**纯文本模型调用，返回 {"text", "usage"}。

    工具调用即协议错误（RelayProtocolError）；阶段超时、输出超限抛
    RelayLimitError。绝不 dispatch 任何工具。provider.stream 的第二个参数
    固定为空列表——这是「无工具」的结构性保证，不是约定。
    """
    parts: list[str] = []
    length = 0
    usage = {"input_tokens": 0, "output_tokens": 0}
    tool_name = ""

    async def _consume() -> None:
        nonlocal length, tool_name
        async for ev in provider.stream(messages, []):
            if isinstance(ev, ProviderToolUse):
                tool_name = ev.name
                return  # 协议错误：立即停止消费，不 dispatch
            if isinstance(ev, ProviderTextDelta):
                parts.append(ev.text)
                length += len(ev.text)
                if length > max_output_chars:
                    raise RelayLimitError(
                        "relay_output_limit", f"节点输出超过上限（{max_output_chars} 字符）"
                    )
            elif isinstance(ev, ProviderDone):
                usage["input_tokens"] = int(getattr(ev, "input_tokens", 0) or 0)
                usage["output_tokens"] = int(getattr(ev, "output_tokens", 0) or 0)
                break

    try:
        async with asyncio.timeout(max(1, int(timeout_s))):
            await _consume()
    except TimeoutError as exc:
        raise RelayLimitError("relay_timeout", f"节点阶段超时（{int(timeout_s)} 秒）") from exc
    if tool_name:
        raise RelayProtocolError(
            f"节点请求了工具「{tool_name}」：接力为无工具纯文本流程，已暂停"
        )
    return {"text": "".join(parts), "usage": usage}
