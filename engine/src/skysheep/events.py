"""Agent 运行过程的统一事件流。

Agent 循环、Provider 流式输出、权限交互、工具执行全部产生事件；
CLI 与未来的 GUI/WebSocket 消费同一套事件，保证行为一致。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


class Event(BaseModel):
    kind: str
    ts: float = Field(default_factory=time.time)


class TurnStarted(Event):
    """一轮模型调用开始（一次完整的 请求→工具→...→回复 片段中的一步）。"""

    kind: Literal["turn_started"] = "turn_started"
    iteration: int = 0


class TextDelta(Event):
    """助手文本增量（流式）。"""

    kind: Literal["text_delta"] = "text_delta"
    text: str = ""


class ThinkingDelta(Event):
    """思考型模型的推理内容增量（流式）。

    思考先于正文产出；前端渲染为可折叠的「思考过程」块，
    正文首个增量到达时自动折叠。
    """

    kind: Literal["thinking_delta"] = "thinking_delta"
    text: str = ""


class AssistantMessage(Event):
    """本迭代完整助手消息（已并入历史）。"""

    kind: Literal["assistant_message"] = "assistant_message"
    message: Any = None  # skysheep.messages.Message，避免循环导入


class ToolCallStarted(Event):
    kind: Literal["tool_call_started"] = "tool_call_started"
    tool_call_id: str = ""
    name: str = ""
    input: dict = Field(default_factory=dict)


class ToolCallFinished(Event):
    kind: Literal["tool_call_finished"] = "tool_call_finished"
    tool_call_id: str = ""
    name: str = ""
    preview: str = ""  # 结果预览（截断后）
    diff: str = ""  # 文件变更 unified diff（仅写文件类工具携带）
    images: list[dict] = Field(default_factory=list)  # 工具附带图片（screenshot），前端内联展示
    is_error: bool = False
    duration_ms: int = 0


class TodoUpdated(Event):
    """Agent 的任务清单发生变化（todo_write 工具）。"""

    kind: Literal["todo_updated"] = "todo_updated"
    items: list[dict] = Field(default_factory=list)  # [{content, status}]


class PermissionRequest(Event):
    """需要用户决策的敏感操作；通过 Agent.respond_permission() 回传决策。"""

    kind: Literal["permission_request"] = "permission_request"
    request_id: str = ""
    tool_name: str = ""
    input: dict = Field(default_factory=dict)
    safety: str = "write"
    detail: str = ""  # 人类可读的操作说明
    diff: str = ""    # 写入类工具的改前→改后预览（write_file / edit_file，可能为空）
    note: str = ""    # 额外说明：例如白名单前缀为何没命中的命令（shell 拼接）
    rule_kind: str = ""     # 选「总是允许」将写入的规则类型（always/prefix/exact/glob）
    rule_pattern: str = ""  # 对应参数；空 = 整个工具（与 gate.rule_for 的产物一致）


class PermissionResolved(Event):
    kind: Literal["permission_resolved"] = "permission_resolved"
    request_id: str = ""
    decision: str = ""  # allow_once / allow_always / deny


class Usage(Event):
    kind: Literal["usage"] = "usage"
    input_tokens: int = 0
    output_tokens: int = 0


class TurnFinished(Event):
    kind: Literal["turn_finished"] = "turn_finished"
    stop_reason: str = "end_turn"  # end_turn / max_iterations / aborted
    iterations: int = 0


class CompactionEvent(Event):
    """上下文压缩完成：旧历史已被摘要替换。"""

    kind: Literal["compaction"] = "compaction"
    before_messages: int = 0
    after_messages: int = 0
    summary_chars: int = 0


class ErrorEvent(Event):
    kind: Literal["error"] = "error"
    message: str = ""


class NoticeEvent(Event):
    """非致命的过程提示（如模型调用自动重试中），前端/CLI 以弱化样式展示。"""

    kind: Literal["notice"] = "notice"
    message: str = ""


class QueueUpdated(Event):
    """消息排队状态变化：pending 为当前仍在排队的轮数。"""

    kind: Literal["queue_updated"] = "queue_updated"
    pending: int = 0


class ScheduleUpdated(Event):
    """日程数据发生变化（schedule_write 工具或界面增删改），前端刷新日程面板。"""

    kind: Literal["schedule_updated"] = "schedule_updated"


class RoundtableStarted(Event):
    """圆桌轮开始：多模型并行独立作答。"""

    kind: Literal["roundtable_started"] = "roundtable_started"
    members: list[dict] = Field(default_factory=list)  # [{index, provider, model}]


class RoundtableMemberDelta(Event):
    """圆桌成员草稿增量（流式）。"""

    kind: Literal["roundtable_member_delta"] = "roundtable_member_delta"
    member_index: int = 0
    text: str = ""


class RoundtableMemberFinished(Event):
    """圆桌成员作答结束：status=done 时 output_tokens 有效，error 时携带错误摘要。"""

    kind: Literal["roundtable_member_finished"] = "roundtable_member_finished"
    member_index: int = 0
    status: str = "done"  # done | error
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


AgentEvent = (
    TurnStarted
    | TextDelta
    | ThinkingDelta
    | AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
    | PermissionRequest
    | PermissionResolved
    | Usage
    | TodoUpdated
    | CompactionEvent
    | TurnFinished
    | ErrorEvent
    | NoticeEvent
    | QueueUpdated
    | ScheduleUpdated
    | RoundtableStarted
    | RoundtableMemberDelta
    | RoundtableMemberFinished
)
