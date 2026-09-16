"""Permission Gate 测试。"""

from __future__ import annotations

from skysheep.security.gate import Decision, PermissionGate, WhitelistRule
from skysheep.tools import EditFileTool, ReadFileTool, RunCommandTool, WriteFileTool


async def test_readonly_auto_allowed():
    gate = PermissionGate()
    pending = await gate.authorize(ReadFileTool(), {"path": "a.txt"})
    assert pending is None


async def test_write_requires_confirmation():
    gate = PermissionGate()
    pending = await gate.authorize(WriteFileTool(), {"path": "a.txt"})
    assert pending is not None
    assert pending.request_id


async def test_allow_once_does_not_persist():
    gate = PermissionGate()
    p1 = await gate.authorize(WriteFileTool(), {"path": "a.txt"})
    p1.resolve(Decision.ALLOW_ONCE)
    assert await p1.wait() == Decision.ALLOW_ONCE
    p2 = await gate.authorize(WriteFileTool(), {"path": "a.txt"})
    assert p2 is not None  # 仍需确认


async def test_allow_always_persists_in_store(store):
    project = await store.get_or_create_project("/tmp/demo-proj")
    gate = PermissionGate(store=store, project_id=project.id)
    await gate.load_project_rules()

    tool = WriteFileTool()
    tool_input = {"path": "a.txt"}
    p = await gate.authorize(tool, tool_input)
    assert p is not None
    p.resolve(Decision.ALLOW_ALWAYS)
    await gate.persist_rule(gate.rule_for(tool, tool_input))

    # 同一 gate：直接放行
    assert await gate.authorize(tool, tool_input) is None
    # 新 gate（模拟新会话）加载项目规则后：同样放行
    gate2 = PermissionGate(store=store, project_id=project.id)
    await gate2.load_project_rules()
    assert await gate2.authorize(tool, tool_input) is None
    # 其他工具不受影响
    assert await gate2.authorize(RunCommandTool(), {"command": "git status"}) is not None


async def test_run_command_prefix_rule():
    gate = PermissionGate()
    tool = RunCommandTool()
    rule = gate.rule_for(tool, {"command": "git status --short"})
    assert rule.kind == "prefix"
    assert rule.pattern == "git status"  # 前两个词
    gate.add_session_rule(rule)
    # 同前缀的命令放行
    assert await gate.authorize(tool, {"command": "git status --short"}) is None
    # 不同前缀仍需确认
    assert await gate.authorize(tool, {"command": "git push -f"}) is not None
    # 语义文本就是命令本身
    assert tool.arg_text({"command": "npm test"}) == "npm test"


async def test_deny_and_session_rules():
    gate = PermissionGate()
    gate.add_session_rule(WhitelistRule(tool="write_file", kind="always"))
    assert await gate.authorize(WriteFileTool(), {"path": "x"}) is None


async def test_write_diff_preview(tmp_path):
    """write_file 确认请求携带改前→改后 diff；新建文件、覆盖文件均有预览。"""
    gate = PermissionGate(working_dir=tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    p = await gate.authorize(WriteFileTool(), {"path": str(f), "content": "hi\n"})
    assert p is not None
    assert "-hello" in p.diff and "+hi" in p.diff
    # 新文件：改前为空
    p2 = await gate.authorize(WriteFileTool(), {"path": str(tmp_path / "new.txt"), "content": "x\n"})
    assert p2 is not None and "+x" in p2.diff and p2.diff.startswith("---")


async def test_edit_diff_preview_and_outside_dir(tmp_path):
    """edit_file 模拟替换生成预览；工作目录之外与匹配失败时不给预览（降级）。"""
    gate = PermissionGate(working_dir=tmp_path)
    f = tmp_path / "b.txt"
    f.write_text("aaa\nbbb\n", encoding="utf-8")
    p = await gate.authorize(EditFileTool(),
                            {"path": str(f), "old_string": "bbb", "new_string": "ccc"})
    assert p is not None and "-bbb" in p.diff and "+ccc" in p.diff
    # 目录之外：不给预览
    outside = tmp_path.parent / "evil.txt"
    p2 = await gate.authorize(WriteFileTool(), {"path": str(outside), "content": "x"})
    assert p2 is not None and p2.diff == ""
    # old_string 不在文件里：预览降级为空
    p3 = await gate.authorize(EditFileTool(),
                             {"path": str(f), "old_string": "zzz", "new_string": "y"})
    assert p3 is not None and p3.diff == ""
