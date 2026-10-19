"""接力配置 v1：固定展示槽位与受限串行图；本模块不授予执行权限。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RelayValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RelaySlot(RelayValue):
    role: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="", max_length=120)
    model: str = Field(default="", max_length=240)
    screen_preference: str = Field(default="auto", min_length=1, max_length=240)
    capabilities: list[str] = Field(default_factory=list, max_length=0)
    execution_mode: Literal["propose"] = "propose"
    context_scope: Literal["user_input", "previous_report"] = "previous_report"
    handoff_policy: Literal["user_confirm"] = "user_confirm"
    completion_signal: Literal["structured_report"] = "structured_report"
    output_subscription: Literal["report"] = "report"

    @model_validator(mode="after")
    def valid_identity(self):
        if not self.role.strip():
            raise ValueError("槽位角色不能为空白")
        if bool(self.provider.strip()) != bool(self.model.strip()):
            raise ValueError("服务与模型须同时指定，或同时留空以沿用当前模型")
        if self.provider != self.provider.strip() or self.model != self.model.strip():
            raise ValueError("服务与模型不能包含首尾空白")
        return self


class RelayNode(RelayValue):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    slot: Literal["A", "B"]
    prompt: str = Field(min_length=1, max_length=8000)
    terminal: bool = False

    @model_validator(mode="after")
    def valid_prompt(self):
        if not self.prompt.strip():
            raise ValueError("节点提示词不能为空白")
        return self


class RelayEdge(RelayValue):
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    input_format: Literal["report"] = "report"
    trigger: Literal["node_completed"] = "node_completed"
    require_confirmation: Literal[True] = True
    failure_policy: Literal["pause"] = "pause"


class RelayWorkflow(RelayValue):
    start_node: str = "research"
    nodes: list[RelayNode] = Field(default_factory=lambda: [
        RelayNode(id="research", slot="A", prompt="根据用户资料形成研究报告"),
        RelayNode(id="review", slot="B", prompt="复核上一节点报告", terminal=True),
    ], min_length=1, max_length=32)
    edges: list[RelayEdge] = Field(default_factory=lambda: [
        RelayEdge(source="research", target="review"),
    ], max_length=31)
    max_steps: int = Field(default=32, ge=1, le=100)
    token_budget: int = Field(default=100000, ge=1024, le=10000000)
    max_no_progress: int = Field(default=2, ge=1, le=10)
    max_retries: int = Field(default=3, ge=1, le=5)
    termination_condition: Literal["terminal_node"] = "terminal_node"

    @model_validator(mode="after")
    def serial_graph(self):
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("节点 ID 不得重复")
        if self.start_node not in nodes:
            raise ValueError("起始节点未定义")
        outgoing: dict[str, str] = {}
        incoming: set[str] = set()
        for edge in self.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise ValueError("连线引用了未定义的节点")
            if edge.source in outgoing or edge.target in incoming:
                raise ValueError("unsupported：本版仅支持串行图，不支持分叉或汇合")
            if nodes[edge.source].terminal:
                raise ValueError("终止节点不能有出边")
            outgoing[edge.source] = edge.target
            incoming.add(edge.target)
        seen: set[str] = set()
        current = self.start_node
        while True:
            if current in seen:
                raise ValueError("unsupported：本版不支持循环")
            seen.add(current)
            if current not in outgoing:
                if not nodes[current].terminal:
                    raise ValueError("路径末端必须声明为终止节点")
                break
            current = outgoing[current]
        if seen != set(nodes):
            raise ValueError("存在从起始节点不可达的节点")
        if self.max_steps < len(seen):
            raise ValueError("max_steps 小于串行路径长度")
        return self


class RelayConfig(RelayValue):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    window_layout: Literal["auto", "dual", "single"] = "auto"
    slots: dict[str, RelaySlot] = Field(default_factory=lambda: {
        "A": RelaySlot(role="研究"), "B": RelaySlot(role="复核"),
    })
    workflow: RelayWorkflow = Field(default_factory=RelayWorkflow)
    handoff_ratio: float = Field(default=0.75, ge=0.5, le=0.9)
    reserve_tokens: int = Field(default=8192, ge=1024, le=1000000)
    recent_tokens: int = Field(default=4096, ge=256, le=100000)
    phase_timeout_seconds: int = Field(default=90, ge=10, le=600)
    handoff_timeout_seconds: int = Field(default=360, ge=30, le=1800)

    @model_validator(mode="after")
    def fixed_slots(self):
        if set(self.slots) != {"A", "B"}:
            raise ValueError("必须且只能配置固定槽位 A、B")
        if self.handoff_timeout_seconds < self.phase_timeout_seconds:
            raise ValueError("总交接超时不能小于单阶段超时")
        if self.recent_tokens > self.reserve_tokens:
            raise ValueError("近期上下文预算不能大于预留预算")
        return self
