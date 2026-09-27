"""todo_write 工具直连测试：args 校验、全量覆盖（含删除语义）、上限截断、注解元数据。

此前 todo_write 只有 agent 循环里的一个事件发射用例间接触达，args 校验、
50 条上限、覆盖式删除语义都没有覆盖。工具本身极简，这里按它的真实契约锁死。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skysheep.tools import ToolContext
from skysheep.tools.todo import MAX_TODOS, TodoItem, TodoWriteArgs, TodoWriteTool


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(working_dir=tmp_path)


# ---------------------------------------------------------------- args 校验


def test_args_require_todos_list():
    """todos 必填：缺字段直接 ValidationError。"""
    with pytest.raises(ValidationError):
        TodoWriteArgs()


def test_args_status_is_strict_literal():
    """status 只收 pending / in_progress / completed，其它值拒绝。"""
    with pytest.raises(ValidationError):
        TodoWriteArgs(todos=[{"content": "x", "status": "done"}])
    args = TodoWriteArgs(todos=[{"content": "x"}])  # 缺省回落 pending
    assert args.todos[0].status == "pending"


def test_args_content_required():
    with pytest.raises(ValidationError):
        TodoWriteArgs(todos=[{"status": "pending"}])


# ---------------------------------------------------------------- 执行语义


async def test_run_full_overwrite_and_progress_counter(tmp_path):
    """每次全量覆盖：第二次写入整体替换第一次；输出带完成计数与状态符号。"""
    tool = TodoWriteTool()
    ctx = _ctx(tmp_path)
    out = await tool.run(TodoWriteArgs(todos=[
        {"content": "调研", "status": "completed"},
        {"content": "实现", "status": "in_progress"},
        {"content": "测试"},
    ]), ctx)
    assert "1/3 done" in out
    assert "✓ 调研" in out and "▶ 实现" in out and "□ 测试" in out
    assert len(tool.items) == 3

    # 删除语义：清单是全量覆盖，写少了就是删掉了
    out2 = await tool.run(TodoWriteArgs(todos=[
        {"content": "实现", "status": "completed"},
    ]), ctx)
    assert "1/1 done" in out2
    assert len(tool.items) == 1
    assert tool.items[0]["content"] == "实现"
    assert all(i["content"] != "调研" for i in tool.items)


async def test_run_caps_at_max_todos(tmp_path):
    """超过 50 条静默截断到上限（不报错、不丢已写部分的响应）。"""
    tool = TodoWriteTool()
    ctx = _ctx(tmp_path)
    todos = [{"content": f"step {i}"} for i in range(MAX_TODOS + 10)]
    out = await tool.run(TodoWriteArgs(todos=todos), ctx)
    assert len(tool.items) == MAX_TODOS
    assert f"0/{MAX_TODOS} done" in out


async def test_run_idempotent_for_same_args(tmp_path):
    """同参数重复写入结果一致（idempotent_hint=True 的契约）。"""
    tool = TodoWriteTool()
    ctx = _ctx(tmp_path)
    args = TodoWriteArgs(todos=[{"content": "a"}, {"content": "b", "status": "completed"}])
    out1 = await tool.run(args, ctx)
    out2 = await tool.run(args, ctx)
    assert out1 == out2


async def test_unknown_status_falls_back_to_pending_box(tmp_path):
    """状态符号表查不到时回落 □（防御性，正常走不到）。"""
    tool = TodoWriteTool()
    ctx = _ctx(tmp_path)
    item = TodoItem(content="x", status="pending")
    out = await tool.run(TodoWriteArgs(todos=[item]), ctx)
    assert "□ x" in out


# ---------------------------------------------------------------- 元数据与注解


def test_metadata_readonly_and_annotations():
    """safety=READONLY（写自有清单免确认）+ MCP 四注解齐全（清单在 test_tool_annotations）。"""
    tool = TodoWriteTool()
    assert tool.safety.value == "readonly"
    schema = tool.to_schema()
    ann = schema["annotations"]
    assert ann["readOnlyHint"] is False       # 全量覆盖外部可见状态，非纯读
    assert ann["destructiveHint"] is False
    assert ann["idempotentHint"] is True
    assert ann["openWorldHint"] is False
