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


class TaskEstimate(Event):
    """接手任务时的耗时预估（core/estimate.py：启发式 + 历史实测校准）。

    先于本轮任何输出事件到达；前端显示「预计 X~Y 分钟」并在运行中对照
    已用时。basis 是人类可读的预估依据（悬停可见）。
    """

    kind: Literal["task_estimate"] = "task_estimate"
    min_seconds: int = 0
    max_seconds: int = 0
    level: str = "normal"  # trivial / light / normal / moderate / heavy / major
    basis: str = ""


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
    # Mod（实验性）的附加说明：带「[Mod·<id>]」来源前缀，前端独立样式区渲染，
    # 不与引擎自产的 note 混排（第三方文本不上无标识的确认卡）
    mod_note: str = ""


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
    # 本轮实测耗时（毫秒）：前端据此把「已用时」芯片定格成引擎真实值，
    # 而不是前端自己从收到预估事件那一刻起的本地计时（两者会有偏差）。
    duration_ms: int = 0


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
    rounds: int = 1  # 总轮数（1=只独立作答；2=含一轮辩论修订）


class RoundtableMemberDelta(Event):
    """圆桌成员草稿增量（流式）。"""

    kind: Literal["roundtable_member_delta"] = "roundtable_member_delta"
    member_index: int = 0
    text: str = ""
    round: int = 0  # 0=独立作答轮；>=1=辩论修订轮（前端据此重置卡片文本）


class RoundtableMemberFinished(Event):
    """圆桌成员作答结束：status=done 时 output_tokens 有效，error 时携带错误摘要。

    skipped=True：辩论修订轮里该成员草稿已收敛（上一轮没改动）而跳过了修订，
    本轮用量为 0；前端据此把卡片标为「已收敛」并完成本轮结算。
    """

    kind: Literal["roundtable_member_finished"] = "roundtable_member_finished"
    member_index: int = 0
    status: str = "done"  # done | error
    error: str = ""
    round: int = 0
    skipped: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


class ModUI(Event):
    """Mod（实验性）产出的声明式展示片段。

    Mod 的 JS 永不进前端——这里是引擎侧 Mod 产出、由固定渲染器落地的词表片段：
    widget 形状见 core/mods.py 的 WIDGET_SLOTS（stat/badge/progress/timeline/text）。
    不进 StreamDeltaMerger 的可合并增量集合：全量状态帧非增量，非增量事件到达
    自动先冲刷缓冲，顺序语义免费获得；每轮上限 50 条由 Agent 循环计数。
    """

    kind: Literal["mod_ui"] = "mod_ui"
    mod_id: str = ""
    session_id: str = ""
    slot: str = "stream"  # tray / stream / perm
    widget: dict = Field(default_factory=dict)


class TeamStarted(Event):
    """团队开工：组队完成，名册与总管形态就绪（与圆桌并列的第二种多模型协作，docs/团队模式设计.md）。"""

    kind: Literal["team_started"] = "team_started"
    roster: list[dict] = Field(default_factory=list)  # [{index, name, provider, model, persona}]
    director_mode: str = "user"  # user（用户总管）/ ai（AI 总管，二期）
    # AI 总管标识（二期）：指定担任总管的 provider/model。用户总管模式下为空串
    # （一期形状不变：前端按空值回退「用户总管」渲染，一期 meta/回放不受影响）
    director_provider: str = ""
    director_model: str = ""


class TeamMessageDelta(Event):
    """团队成员发言增量（流式）。

    可合并增量：消费端须把它登记进 server/backend.py 的 _MERGEABLE_DELTA_KINDS，
    并按 member_index 分桶合并（沿圆桌 roundtable_member_delta 的分桶先例）。
    member_index = -1 表示 AI 总管（二期）：合并器按 (member_index, seq) 分桶，
    负数键天然兼容；前端据此把总管发言与队员发言分流渲染。
    """

    kind: Literal["team_message_delta"] = "team_message_delta"
    member_index: int = 0  # 名册位次；-1 = AI 总管（二期）
    seq: int = 0  # 所属频道消息编号（定稿见 TeamMessage）
    text: str = ""


class TeamMessage(Event):
    """团队频道消息定稿（全员共享消息簿，seq 单调递增）。"""

    kind: Literal["team_message"] = "team_message"
    seq: int = 0
    from_member: str = "system"  # 成员名 / director / user / system
    to_member: str = "all"  # all（广播）/ 成员名（@定向）/ director
    # 消息类别。不叫 kind：基类 Event.kind 是事件判别字段（Literal["team_message"]），
    # 子类再声明 kind 会把它覆盖掉，毁掉整条按 kind 分发的事件链。
    msg_kind: str = "system"  # assign / report / ask / help / object / ruling / system
    task_ref: str = ""  # 关联工单编号（可空）
    text: str = ""


class TeamTaskUpdated(Event):
    """工单板变化：派工、状态流转、打回计数（状态机见 docs/团队模式设计.md §5）。"""

    kind: Literal["team_task_updated"] = "team_task_updated"
    task_id: str = ""
    title: str = ""
    assignee: str = ""
    type: str = "exec"  # exec（执行型，带工具）/ advisor（顾问型，只出意见）——工单级属性
    status: str = "pending"  # pending / in_progress / review / done / error（待办/进行中/待验收/完成/失败）
    redo: int = 0  # 被打回次数


class TeamFinished(Event):
    """团队终态：交付或终止，summary 携带《交付说明》。"""

    kind: Literal["team_finished"] = "team_finished"
    status: str = "done"  # done / aborted / rounds_exhausted / budget_exhausted（预算越线强制交付，二期）
    summary: str = ""


class AdversarialStarted(Event):
    """对抗轮开始：四角色流水线就绪（core/adversarial.py，docs/对抗模式设计.md）。

    与圆桌（会诊融合）、团队（分工协作）并列的第三种多模型协作形态。
    roles 形如 [{role, provider, model}]，role 取 finder / investigator /
    advisor / judge（缺省裁判为当前主模型，同圆桌主席）。
    """

    kind: Literal["adversarial_started"] = "adversarial_started"
    roles: list[dict] = Field(default_factory=list)


class AdversarialPhase(Event):
    """对抗阶段切换：finder → investigator → advisor → judge。"""

    kind: Literal["adversarial_phase"] = "adversarial_phase"
    phase: str = "finder"
    note: str = ""  # 人类可读的进度说明（如「12 条候选问题待验证」）


class AdversarialFindingProposed(Event):
    """发现者提出一条候选问题（召回优先；裁决见 AdversarialVerdict）。"""

    kind: Literal["adversarial_finding_proposed"] = "adversarial_finding_proposed"
    finding_id: str = ""
    category: str = ""
    severity: str = ""
    location: str = ""
    description: str = ""


class AdversarialVerdict(Event):
    """调查员对一条候选问题的裁决（对抗验证，防守方复核）。"""

    kind: Literal["adversarial_verdict"] = "adversarial_verdict"
    finding_id: str = ""
    verdict: str = ""  # confirmed / refuted / partial
    reason: str = ""


class AdversarialFinished(Event):
    """对抗终态：status=done 时 summary 携带裁决统计。"""

    kind: Literal["adversarial_finished"] = "adversarial_finished"
    status: str = "done"  # done / error / cancelled
    summary: str = ""


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
    | TaskEstimate
    | CompactionEvent
    | TurnFinished
    | ErrorEvent
    | NoticeEvent
    | QueueUpdated
    | ScheduleUpdated
    | RoundtableStarted
    | RoundtableMemberDelta
    | RoundtableMemberFinished
    | ModUI
    | TeamStarted
    | TeamMessageDelta
    | TeamMessage
    | TeamTaskUpdated
    | TeamFinished
    | AdversarialStarted
    | AdversarialPhase
    | AdversarialFindingProposed
    | AdversarialVerdict
    | AdversarialFinished
)
