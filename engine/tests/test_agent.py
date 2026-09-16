"""Agent 核心循环测试（FakeProvider 脚本驱动）。"""

from __future__ import annotations

import json

from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools


def make_agent(provider, tmp_path, store=None, project_id=None, max_iterations=10):
    gate = PermissionGate(store=store, project_id=project_id)
    registry = ToolRegistry(default_tools())
    return Agent(
        provider=provider,
        registry=registry,
        gate=gate,
        working_dir=tmp_path,
        max_iterations=max_iterations,
    )


async def collect(agent, text, auto_respond="allow_once"):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request" and auto_respond:
            agent.respond_permission(ev.request_id, auto_respond)
    return events


async def test_plain_text_turn(tmp_path):
    provider = FakeProvider([[TextBlock(text="你好，世界")]])
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "hi")

    kinds = [e.kind for e in events]
    assert "text_delta" in kinds
    assert kinds[-1] == "turn_finished"
    assert events[-1].stop_reason == "end_turn"
    assert events[-1].iterations == 1
    # 历史：user + assistant
    assert [m.role for m in agent.history] == ["user", "assistant"]
    assert agent.history[1].text == "你好，世界"
    # token 统计
    assert agent.total_in_tokens == 11


async def test_tool_call_flow_writes_file(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "a.txt", "content": "hi"})],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "create a.txt with hi")

    # 文件真实写入
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi"
    kinds = [e.kind for e in events]
    assert "permission_request" in kinds          # write 需要确认
    assert "permission_resolved" in kinds
    assert "tool_call_started" in kinds
    finished = [e for e in events if e.kind == "tool_call_finished"]
    assert finished and not finished[0].is_error
    assert "a.txt" in finished[0].preview
    # 两轮迭代后正常结束
    assert events[-1].stop_reason == "end_turn"
    assert events[-1].iterations == 2
    # 历史：user, assistant(tool_use), tool, assistant(text)
    roles = [m.role for m in agent.history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # 工具结果回到了 provider 的第二次调用里
    second_call = provider.calls[1]
    assert any(m.role == "tool" for m in second_call)


async def test_denied_tool_returns_error_result(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "b.txt", "content": "x"})],
            [TextBlock(text="ok, skipped")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "create b.txt", auto_respond="deny")

    assert not (tmp_path / "b.txt").exists()
    tool_msg = [m for m in agent.history if m.role == "tool"][0]
    block = tool_msg.content[0]
    assert block.is_error
    assert "denied" in block.content.lower()
    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert finished.is_error


async def test_readonly_tool_needs_no_permission(tmp_path):
    (tmp_path / "c.txt").write_text("findme", encoding="utf-8")
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="grep", input={"pattern": "findme"})],
            [TextBlock(text="found in c.txt")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "search findme", auto_respond=None)

    kinds = [e.kind for e in events]
    assert "permission_request" not in kinds  # 只读自动放行
    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert "c.txt:1" in finished.preview


async def test_allow_always_whitelists_subsequent_calls(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "x1.txt", "content": "1"})],
            [ToolUseBlock(id="t2", name="write_file", input={"path": "x2.txt", "content": "2"})],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    reqs = []
    async for ev in agent.run_turn("write two files"):
        if ev.kind == "permission_request":
            reqs.append(ev.request_id)
            agent.respond_permission(ev.request_id, "allow_always")

    assert (tmp_path / "x1.txt").exists() and (tmp_path / "x2.txt").exists()
    assert len(reqs) == 1  # 第二次写文件被白名单放行，不再询问


async def test_unknown_tool_reported_as_error(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="nonexistent_tool", input={})],
            [TextBlock(text="ok")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    await collect(agent, "use magic", auto_respond=None)
    tool_msg = [m for m in agent.history if m.role == "tool"][0]
    assert tool_msg.content[0].is_error
    assert "unknown tool" in tool_msg.content[0].content


async def test_max_iterations_stops(tmp_path):
    tu = [ToolUseBlock(id="loop", name="list_dir", input={})]
    provider = FakeProvider([tu]).with_default(tu)
    agent = make_agent(provider, tmp_path, max_iterations=3)
    events = await collect(agent, "loop", auto_respond=None)

    assert events[-1].kind == "turn_finished"
    assert events[-1].stop_reason == "max_iterations"
    assert events[-1].iterations == 3


async def test_history_includes_tool_args_for_provider(tmp_path):
    payload = {"path": "j.json", "content": json.dumps({"a": 1})}
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input=payload)],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    await collect(agent, "write json", auto_respond="allow_once")

    second_call_messages = provider.calls[1]
    assistant_msg = [m for m in second_call_messages if m.role == "assistant"][0]
    assert assistant_msg.tool_uses[0].input == {"path": "j.json", "content": '{"a": 1}'}
    tool_msg = [m for m in second_call_messages if m.role == "tool"][0]
    assert not tool_msg.content[0].is_error


async def test_todo_write_emits_event(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="td", name="todo_write", input={"todos": [
                {"content": "步骤1", "status": "completed"},
                {"content": "步骤2", "status": "in_progress"},
            ]})],
            [TextBlock(text="ok")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "跑任务", auto_respond=None)

    todo_events = [e for e in events if e.kind == "todo_updated"]
    assert todo_events, "应产出 todo_updated 事件"
    items = todo_events[0].items
    assert [i["content"] for i in items] == ["步骤1", "步骤2"]
    assert items[0]["status"] == "completed"


async def test_write_file_carries_diff(tmp_path):
    (tmp_path / "d.txt").write_text("old line\n", encoding="utf-8")
    provider = FakeProvider(
        [
            [ToolUseBlock(id="w1", name="write_file", input={
                "path": "d.txt", "content": "new line\nanother\n",
            })],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "覆盖文件", auto_respond="allow_once")

    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert "-old line" in finished.diff
    assert "+new line" in finished.diff
    assert "a/d.txt" in finished.diff


async def test_edit_file_carries_diff(tmp_path):
    (tmp_path / "e.py").write_text("def main():\n    pass\n", encoding="utf-8")
    provider = FakeProvider(
        [
            [ToolUseBlock(id="e1", name="edit_file", input={
                "path": "e.py", "old_string": "pass", "new_string": "print('hi')",
            })],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "编辑文件", auto_respond="allow_once")

    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert "-    pass" in finished.diff
    assert "+    print('hi')" in finished.diff
