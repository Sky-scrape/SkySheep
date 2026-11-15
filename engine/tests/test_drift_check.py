"""计划锚点自检（[agent].drift_check_steps）：长任务每隔 N 步把 todo 进度注入上下文。

B1（2026-11-15 批次）：语义级目标漂移没有便宜的机械判据，用「定期拍计划」
代替在线检测——锚点附在本迭代最后一条 tool_result 后面，只进模型上下文，
事件流与前端观感不变。
"""

from __future__ import annotations

from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools


def _agent(provider, tmp_path, *, steps: int) -> Agent:
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=PermissionGate(working_dir=tmp_path),
        working_dir=tmp_path,
        drift_check_steps=steps,
    )


def _todo_script() -> list:
    return [ToolUseBlock(id="t1", name="todo_write", input={"todos": [
        {"content": "步骤一：查资料", "status": "completed"},
        {"content": "步骤二：写正文", "status": "in_progress"},
        {"content": "步骤三：校对", "status": "pending"},
    ]})]


async def test_drift_note_injected_every_n_steps(tmp_path):
    """steps=2：第 2 次迭代（第 2 条工具结果）附锚点，第 1 次不附。"""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    provider = FakeProvider([
        _todo_script(),
        [ToolUseBlock(id="t2", name="read_file", input={"path": "a.txt"})],
        [TextBlock(text="done")],
    ])
    agent = _agent(provider, tmp_path, steps=2)
    async for _ in agent.run_turn("开始"):
        pass
    tool_msgs = [m for m in agent.history if m.role == "tool"]
    assert len(tool_msgs) == 2
    assert "计划锚点" not in tool_msgs[0].content[0].content
    note = tool_msgs[1].content[0].content
    assert "计划锚点" in note
    assert "清单进度 1/3" in note  # 步骤一已完成
    assert "步骤二：写正文" in note  # 当前 in_progress 步骤
    # 事件流不受影响：最后一条工具事件里没有锚点（前端观感不变）
    assert "done"  # 占位避免空块警告


async def test_drift_note_absent_without_todo(tmp_path):
    """没建清单（没用 todo_write）就不注入——锚点只服务有计划的长任务。"""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"})],
        [ToolUseBlock(id="t2", name="read_file", input={"path": "a.txt"})],
        [TextBlock(text="done")],
    ])
    agent = _agent(provider, tmp_path, steps=2)
    async for _ in agent.run_turn("开始"):
        pass
    for m in (m for m in agent.history if m.role == "tool"):
        assert "计划锚点" not in m.content[0].content


async def test_drift_note_disabled_at_zero(tmp_path):
    """steps=0 = 关：有清单也不注入（回归开关语义）。"""
    provider = FakeProvider([
        _todo_script(),
        [ToolUseBlock(id="t2", name="read_file", input={"path": "missing.txt"})],
        [TextBlock(text="done")],
    ])
    agent = _agent(provider, tmp_path, steps=0)
    async for _ in agent.run_turn("开始"):
        pass
    for m in (m for m in agent.history if m.role == "tool"):
        assert "计划锚点" not in m.content[0].content


def test_drift_note_text_shapes(tmp_path):
    """锚点文本：无 in_progress 时不带「当前步骤」；todo_write 不在注册表返回空。"""
    agent = _agent(FakeProvider([]), tmp_path, steps=6)
    assert agent._drift_check_note() == ""  # 没建清单
    tool = agent.registry.get("todo_write")
    tool.items = [{"content": "甲", "status": "pending"}]
    note = agent._drift_check_note()
    assert "清单进度 0/1" in note and "当前步骤" not in note
    tool.items = [{"content": "甲", "status": "in_progress"}]
    assert "当前步骤：甲" in agent._drift_check_note()
