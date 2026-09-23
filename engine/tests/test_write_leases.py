"""写租约测试：并行任务的写路径协调（security/leases.py + 权限门 + agent 循环）。

对应能力：同一项目里多会话/流水线/定时任务并行写同一文件时，在权限门领租约
错开执行；冲突等待有上限，超时 fail open 但带注记，让模型与用户看得见。
"""

from __future__ import annotations

import asyncio

from skysheep.core.agent import Agent
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security import leases
from skysheep.security.gate import PermissionGate
from skysheep.tools import ReadFileTool, RunCommandTool, ToolContext, ToolRegistry, WriteFileTool
from skysheep.tools.fs import MoveFileTool

# ---------------------------------------------------------------- 枢红单元


async def test_lease_fast_path_and_release(tmp_path):
    """无冲突：立即拿到；release 后表清空；重复 release 幂等。"""
    hub = leases.WriteLeaseHub()
    lease = await hub.claim([(tmp_path / "a.txt", "a.txt")], owner="s1")
    assert lease.note == ""
    assert len(hub._entries) == 1
    assert lease.release() == ""
    assert hub._entries == {}
    lease.release()  # 幂等
    assert hub._entries == {}


async def test_lease_conflict_times_out_with_note(tmp_path, monkeypatch):
    """跨会话冲突：等待超时后按原计划放行（接管），租约带冲突注记；
    前任持有者后到的 release 不会误删接管者的登记（按身份归还）。"""
    monkeypatch.setattr(leases, "WRITE_LEASE_WAIT_S", 0.05)
    hub = leases.WriteLeaseHub()
    lease_a = await hub.claim([(tmp_path / "x.txt", "x.txt")], owner="sA")
    lease_b = await hub.claim([(tmp_path / "x.txt", "x.txt")], owner="sB")
    assert "并行写入" in lease_b.note and "x.txt" in lease_b.note
    lease_a.release()  # 迟到的归还：自己的登记已被接管，不该动 B 的
    assert len(hub._entries) == 1
    lease_b.release()
    assert hub._entries == {}


async def test_lease_conflict_waits_then_proceeds(tmp_path):
    """对方在超时内释放：等待者拿到租约并带「等待后继续」注记。"""
    hub = leases.WriteLeaseHub()
    lease_a = await hub.claim([(tmp_path / "y.txt", "y.txt")], owner="sA")

    async def release_soon():
        await asyncio.sleep(0.05)
        lease_a.release()

    asyncio.get_running_loop().create_task(release_soon())
    lease_b = await hub.claim([(tmp_path / "y.txt", "y.txt")], owner="sB", wait_s=5.0)
    assert lease_b.note != "" and "等待" in lease_b.note
    lease_b.release()
    assert hub._entries == {}


async def test_lease_same_or_empty_owner_no_conflict(tmp_path):
    """同会话重入与空 owner（CLI 等无归属态）都不等待。"""
    hub = leases.WriteLeaseHub()
    await hub.claim([(tmp_path / "z.txt", "z.txt")], owner="s1")
    lease = await hub.claim([(tmp_path / "z.txt", "z.txt")], owner="s1", wait_s=0.01)
    assert lease.note == ""
    lease2 = await hub.claim([(tmp_path / "z.txt", "z.txt")], owner="", wait_s=0.01)
    assert lease2.note == ""


async def test_lease_ancestor_overlap_both_directions(tmp_path):
    """祖先关系双向算冲突：往正被持有的目录里写、持有文件时整目录被动都等。"""
    hub = leases.WriteLeaseHub()
    # 持有的是目录（delete_file 场景），别人写目录内文件 → 冲突
    lease_dir = await hub.claim([(tmp_path / "docs", "docs")], owner="sA")
    lease_file = await hub.claim(
        [(tmp_path / "docs" / "n.md", "docs/n.md")], owner="sB", wait_s=0.01
    )
    assert "并行写入" in lease_file.note
    lease_dir.release()
    lease_file.release()
    # 反向：持有的是文件，别人整目录移动/删除 → 冲突
    lease_file2 = await hub.claim([(tmp_path / "docs" / "n.md", "n.md")], owner="sA")
    lease_dir2 = await hub.claim([(tmp_path / "docs", "docs")], owner="sB", wait_s=0.01)
    assert "并行写入" in lease_dir2.note
    lease_file2.release()
    lease_dir2.release()


def test_hub_for_is_per_project_singleton(tmp_path):
    """同项目目录（不同 Path 实例）取到同一枢纽；跨目录隔离；无目录为 None。"""
    a = leases.hub_for(tmp_path)
    assert a is leases.hub_for(tmp_path / "." / "sub" / "..")
    assert a is not leases.hub_for(tmp_path.parent / "另一个项目")
    assert leases.hub_for(None) is None


# ---------------------------------------------------------------- 门集成


async def test_gate_claims_only_for_write_tools_with_targets(tmp_path):
    """claim_write 只对声明了写落点的写工具生效；只读与命令工具不参与。"""
    gate = PermissionGate(working_dir=tmp_path)
    assert await gate.claim_write(ReadFileTool(), {"path": "a.txt"}, owner="s1") is None
    assert await gate.claim_write(
        RunCommandTool(), {"command": "echo hi"}, owner="s1"
    ) is None
    lease = await gate.claim_write(WriteFileTool(), {"path": "a.txt"}, owner="s1")
    assert lease is not None and lease.note == ""
    assert len(leases.hub_for(tmp_path)._entries) == 1
    lease.release()


async def test_gate_move_file_claims_source_and_destination(tmp_path):
    """move_file 声明 guard_path_args：源和目标都进租约，别的任务写目标会撞。"""
    gate = PermissionGate(working_dir=tmp_path)
    lease = await gate.claim_write(
        MoveFileTool(), {"source": "a.txt", "destination": "b.txt"}, owner="s1"
    )
    assert lease is not None
    assert len(leases.hub_for(tmp_path)._entries) == 2
    lease_b = await gate.claim_write(WriteFileTool(), {"path": "b.txt"}, owner="s2")
    assert lease_b is not None and "并行写入" in lease_b.note
    lease.release()
    lease_b.release()


async def test_gate_without_workdir_claims_nothing(tmp_path):
    gate = PermissionGate()  # 无项目态
    assert await gate.claim_write(WriteFileTool(), {"path": "a.txt"}, owner="s1") is None


# ---------------------------------------------------------------- agent 循环集成


def _make_agent(tmp_path) -> Agent:
    return Agent(
        provider=FakeProvider([[TextBlock(text="ok")]]),
        registry=ToolRegistry([WriteFileTool(), ReadFileTool()]),
        gate=PermissionGate(working_dir=tmp_path),
        working_dir=tmp_path,
    )


async def test_exec_tool_appends_conflict_note_and_releases(tmp_path, monkeypatch):
    """并行冲突经 _exec_tool 落到工具结果：文件照写、注记可见、租约用完即还。"""
    monkeypatch.setattr(leases, "WRITE_LEASE_WAIT_S", 0.05)
    agent = _make_agent(tmp_path)
    hub = leases.hub_for(tmp_path)
    lease_a = await hub.claim([(tmp_path / "shared.txt", "shared.txt")], owner="sA")

    tu = ToolUseBlock(id="t1", name="write_file",
                      input={"path": "shared.txt", "content": "B 的内容"})
    ctx = ToolContext(working_dir=tmp_path, session_id="sB")
    result, is_error, _ms, _diff = await agent._exec_tool(
        tu, agent.registry.get("write_file"), ctx
    )
    assert is_error is False
    assert "wrote" in result and "并行写入" in result
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") == "B 的内容"
    # B 已归还自己的租约（A 迟到的归还也不留残留）
    lease_a.release()
    assert hub._entries == {}


async def test_exec_tool_readonly_never_claims(tmp_path):
    """只读工具不进租约表：并发批的只读调用零开销。"""
    agent = _make_agent(tmp_path)
    (tmp_path / "r.txt").write_text("hi", encoding="utf-8")
    tu = ToolUseBlock(id="t2", name="read_file", input={"path": "r.txt"})
    ctx = ToolContext(working_dir=tmp_path, session_id="s1")
    result, is_error, _ms, _diff = await agent._exec_tool(
        tu, agent.registry.get("read_file"), ctx
    )
    assert is_error is False and "hi" in result
    assert leases.hub_for(tmp_path)._entries == {}


async def test_exec_tool_tool_error_still_releases(tmp_path):
    """工具报错路径租约也必须归还（finally），不留僵尸租约卡住后续写入。"""
    agent = _make_agent(tmp_path)
    tu = ToolUseBlock(id="t3", name="edit_file",
                      input={"path": "ghost.txt", "old_string": "a", "new_string": "b"})
    ctx = ToolContext(working_dir=tmp_path, session_id="s1")
    from skysheep.tools import EditFileTool

    result, is_error, _ms, _diff = await agent._exec_tool(
        tu, EditFileTool(), ctx
    )
    assert is_error is True
    assert leases.hub_for(tmp_path)._entries == {}
