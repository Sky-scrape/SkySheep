/* SkySheep 引擎事件 kind 常量表——由 engine/tools/gen_frontend_events.py 从
 * src/skysheep/events.py 反射生成，勿手改；手改会被 tests/test_event_kind_contract.py
 * 逐字比对拦下。重新生成：uv run python tools/gen_frontend_events.py
 * 只校验不落盘：uv run python tools/gen_frontend_events.py --check
 * 加载顺序：必须先于 app.js（index.html 尾部 script 块的第一个标签）。
 */
"use strict";

// 引擎 AgentEvent 全部 34 个事件 kind（kind 字典序）。后端 WS 广播的非引擎 kind
// （update_progress / relay_pair_* / session_updated / cron_updated 等）不在此表：
// 它们由 server/backend.py 直接 emit，不经 events.py 事件类，handleEvent 分支里
// 仍是字面量（广播 kind 出仓属后续批次，暂不实施）。
const AP_EVENTS = Object.freeze({
  ADVERSARIAL_FINDING_PROPOSED: "adversarial_finding_proposed",
  ADVERSARIAL_FINISHED: "adversarial_finished",
  ADVERSARIAL_PHASE: "adversarial_phase",
  ADVERSARIAL_STARTED: "adversarial_started",
  ADVERSARIAL_VERDICT: "adversarial_verdict",
  ASSISTANT_MESSAGE: "assistant_message",
  COMPACTION: "compaction",
  ERROR: "error",
  MOD_UI: "mod_ui",
  NOTICE: "notice",
  PERMISSION_REQUEST: "permission_request",
  PERMISSION_RESOLVED: "permission_resolved",
  QUEUE_UPDATED: "queue_updated",
  ROUNDTABLE_MEMBER_DELTA: "roundtable_member_delta",
  ROUNDTABLE_MEMBER_FINISHED: "roundtable_member_finished",
  ROUNDTABLE_STARTED: "roundtable_started",
  SCHEDULE_UPDATED: "schedule_updated",
  SUBAGENT_EVENT: "subagent_event",
  SUBAGENT_SPAWNED: "subagent_spawned",
  TASK_ESTIMATE: "task_estimate",
  TASK_FINISHED: "task_finished",
  TEAM_FINISHED: "team_finished",
  TEAM_MESSAGE: "team_message",
  TEAM_MESSAGE_DELTA: "team_message_delta",
  TEAM_STARTED: "team_started",
  TEAM_TASK_UPDATED: "team_task_updated",
  TEXT_DELTA: "text_delta",
  THINKING_DELTA: "thinking_delta",
  TODO_UPDATED: "todo_updated",
  TOOL_CALL_FINISHED: "tool_call_finished",
  TOOL_CALL_STARTED: "tool_call_started",
  TURN_FINISHED: "turn_finished",
  TURN_STARTED: "turn_started",
  USAGE: "usage",
});
