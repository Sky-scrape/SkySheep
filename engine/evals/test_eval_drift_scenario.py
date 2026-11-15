"""评测基线 · 计划锚点自检（真实 Agent 循环 + 真实注册表）。

场景 37：长任务每隔 ``drift_check_steps`` 步，把 todo 清单进度注入最后一条
tool_result（只进模型上下文，事件流不变）。锚的是「语义级目标漂移没有
便宜的机械判据」——用定期拍计划代替在线检测，每 N 步提醒模型对照原目标。
"""

from __future__ import annotations

from evals._harness import finished, make_agent, run_eval_turn
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider

TODO_SCRIPT = [ToolUseBlock(id="t1", name="todo_write", input={"todos": [
    {"content": "查现有实现", "status": "completed"},
    {"content": "改核心逻辑", "status": "in_progress"},
    {"content": "补测试", "status": "pending"},
]})]


async def test_eval_drift_anchor_injected_after_n_steps(home):
    """场景 37（计划锚点侧）：steps=2 时第 2 条工具结果带锚点，第 1 条不带。"""
    proj = home / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "a.txt").write_text("hello", encoding="utf-8")
    provider = FakeProvider([
        TODO_SCRIPT,
        [ToolUseBlock(id="t2", name="read_file", input={"path": "a.txt"})],
        [TextBlock(text="收到，继续。")],
    ])
    agent = make_agent(provider, proj, drift_check_steps=2)
    events = await run_eval_turn(agent, "开始干活")
    assert events[-1].kind == "turn_finished"

    tool_msgs = [m for m in agent.history if m.role == "tool"]
    assert len(tool_msgs) == 2
    assert "计划锚点" not in tool_msgs[0].content[0].content
    note = tool_msgs[1].content[0].content
    assert "计划锚点" in note and "清单进度 1/3" in note
    # 只进上下文：前端收到的事件流里没有锚点文本
    fin = finished(events)
    assert "计划锚点" not in fin["t2"].preview

    # 锚点提醒行为断言 + 指标预算（读文件 1 次、只读免确认）
    from evals._metrics import TurnMetrics

    m = TurnMetrics.from_events(events)
    assert m.tool_calls == 2 and m.permission_requests == 0


async def test_eval_drift_anchor_absent_without_todo(home):
    """对照侧：没建清单（不用 todo_write）就不注入——锚点只服务有计划的任务。"""
    proj = home / "proj2"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "a.txt").write_text("hello", encoding="utf-8")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"})],
        [ToolUseBlock(id="t2", name="read_file", input={"path": "a.txt"})],
        [TextBlock(text="收到。")],
    ])
    agent = make_agent(provider, proj, drift_check_steps=2)
    await run_eval_turn(agent, "开始干活")
    for m in (m for m in agent.history if m.role == "tool"):
        assert "计划锚点" not in m.content[0].content
