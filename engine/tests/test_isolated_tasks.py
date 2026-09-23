"""隔离派生测试：git worktree 目录级隔离（spawn_agent isolated=true）。

对应能力：并行改文件的后台子任务各自跑在 <项目>/.skysheep/worktrees/<id>
的专用 worktree + 独立分支上，写入不碰主工作区，完成自动提交，报告带
合并指引；非 git 仓库 / 同步派生在派生时即被拒绝。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from skysheep.core.subagent import (
    CheckTaskArgs,
    CheckTaskTool,
    IsolatedGate,
    SpawnAgentTool,
    TaskManager,
)
from skysheep.core.worktree import commit_all, ensure_worktree, remove_worktree
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security import leases
from skysheep.security.gate import Decision
from skysheep.tools import ReadFileTool, RunCommandTool, ToolContext, ToolError, WriteFileTool

GIT = shutil.which("git")


def _init_repo(p: Path) -> None:
    def g(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", *args], cwd=p, check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )

    g("init")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (p / "base.txt").write_text("v1", encoding="utf-8")
    g("add", "-A")
    g("commit", "-m", "init")


async def _wait_status(tasks: TaskManager, task_id: str, status: str, timeout: float = 20.0):
    for _ in range(int(timeout / 0.05)):
        rec = tasks.status(task_id)
        if rec is not None and rec.status == status:
            return rec
        await asyncio.sleep(0.05)
    return tasks.status(task_id)


# ---------------------------------------------------------------- worktree 模块


@pytest.mark.skipif(GIT is None, reason="需要 git")
def test_ensure_worktree_and_commit_all(tmp_path):
    """建 worktree（幂等复用）→ 写文件 → 自动提交拿到 commit id；本地忽略已登记。"""
    _init_repo(tmp_path)
    wt = tmp_path / ".skysheep" / "worktrees" / "t1"
    ensure_worktree(tmp_path, wt, "skysheep/task-t1")
    assert (wt / ".git").exists() and (wt / "base.txt").exists()
    ensure_worktree(tmp_path, wt, "skysheep/task-t1")  # 已存在 → 复用不报错
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".skysheep/worktrees/" in exclude

    (wt / "f.txt").write_text("x", encoding="utf-8")
    cid = commit_all(wt, "测试提交")
    assert cid
    assert commit_all(wt, "无改动") == ""  # nothing to commit → 空串

    with pytest.raises(RuntimeError):
        ensure_worktree(tmp_path.parent, wt.parent / "别处", "skysheep/task-x")  # 非仓库


# ---------------------------------------------------------------- 门控


def test_remove_worktree_clears_registration(tmp_path):
    """M15：移除隔离工作区后，主仓 worktree 列表不再挂着它（不必手动 prune）。"""
    _init_repo(tmp_path)
    wt = tmp_path / ".skysheep" / "worktrees" / "t9"
    ensure_worktree(tmp_path, wt, "skysheep/task-t9")
    (wt / "f.txt").write_text("x", encoding="utf-8")
    cid = commit_all(wt, "部分改动")

    def listing() -> str:
        return subprocess.run(  # noqa: S603
            ["git", "worktree", "list"], cwd=tmp_path,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout

    assert "t9" in listing()
    remove_worktree(tmp_path, wt, "skysheep/task-t9")
    assert not wt.exists(), "工作区目录应被删掉"
    assert "t9" not in listing(), "worktree 注册应被清掉（不需要手动 prune）"
    # 分支保留：取消前的部分改动（已提交）不能跟着目录一起丢
    r = subprocess.run(  # noqa: S603
        ["git", "log", "--oneline", "skysheep/task-t9"], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert cid in r.stdout

    # 目录已被手动删掉时退化为 prune，同样不留悬挂注册
    wt2 = tmp_path / ".skysheep" / "worktrees" / "t10"
    ensure_worktree(tmp_path, wt2, "skysheep/task-t10")
    shutil.rmtree(wt2, ignore_errors=True)
    remove_worktree(tmp_path, wt2, "skysheep/task-t10")
    assert "t10" not in listing()


async def test_cancelled_isolated_task_cleans_worktree(tmp_path, monkeypatch):
    """M15：取消隔离任务时收拾 worktree——部分改动提交到分支，注册清掉。"""
    _init_repo(tmp_path)
    monkeypatch.setattr(leases, "WRITE_LEASE_WAIT_S", 0.05)
    script = [
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "half.py", "content": "print(1)\n"})],
        [TextBlock(text="还没写完")],
    ]
    tasks = TaskManager(provider_factory=lambda: FakeProvider(script), working_dir=tmp_path)
    tool = SpawnAgentTool(tasks)
    out = await tool.run(
        tool.args_model(agent_type="task", prompt="写一半", background=True, isolated=True),
        ToolContext(working_dir=tmp_path, session_id="s1"),
    )
    task_id = out.split("task_id=")[1].split(";")[0]
    rec = await _wait_status(tasks, task_id, "done")
    assert rec is not None

    # 造一个「取消」场景：任务已建好 worktree，随后被取消
    wt = Path(rec.worktree_dir)
    assert wt.is_dir()
    rec.status = "running"
    await tasks._cleanup_isolated(rec)
    assert not wt.exists(), "取消后工作区目录应被清理"
    listing = subprocess.run(  # noqa: S603
        ["git", "worktree", "list"], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    assert str(wt) not in listing

    rec.status = "cancelled"
    note = tasks._isolated_note(rec)
    assert "取消" in note and rec.branch in note


async def test_isolated_gate_allows_workdir_writes_denies_commands(tmp_path):
    """IsolatedGate：只读与工作区内写入放行；越界写入与命令执行预拒绝。"""
    gate = IsolatedGate(working_dir=tmp_path)
    assert await gate.authorize(ReadFileTool(), {"path": "a.txt"}) is None
    assert await gate.authorize(WriteFileTool(), {"path": "in.txt"}) is None

    outside = await gate.authorize(
        WriteFileTool(), {"path": str(tmp_path.parent / "outside.txt")}
    )
    assert outside is not None and outside._future.done()  # 预拒绝，不挂起
    assert "隔离" in outside.deny_note
    assert await outside.wait() == Decision.DENY

    cmd = await gate.authorize(RunCommandTool(), {"command": "echo hi"})
    assert cmd is not None and cmd._future.done()
    assert await cmd.wait() == Decision.DENY


# ---------------------------------------------------------------- 派生校验


async def test_isolated_requires_background(tmp_path):
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="x")]]), working_dir=tmp_path,
    )
    tool = SpawnAgentTool(tasks)
    with pytest.raises(ToolError, match="background"):
        await tool.run(
            tool.args_model(agent_type="task", prompt="x", background=False, isolated=True),
            ToolContext(working_dir=tmp_path, session_id="s1"),
        )


async def test_isolated_requires_git_repo(tmp_path):
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="x")]]), working_dir=tmp_path,
    )
    assert tasks.can_isolate() is False  # tmp_path 没有 .git
    tool = SpawnAgentTool(tasks)
    with pytest.raises(ToolError, match="git"):
        await tool.run(
            tool.args_model(agent_type="task", prompt="x", background=True, isolated=True),
            ToolContext(working_dir=tmp_path, session_id="s1"),
        )


# ---------------------------------------------------------------- 全流程


@pytest.mark.skipif(GIT is None, reason="需要 git")
async def test_isolated_task_runs_in_worktree_and_commits(tmp_path, monkeypatch):
    """端到端：隔离任务在 worktree 里写文件，主工作区不被触碰，完成自动提交，
    check_task 报告带工作区/分支/合并指引。"""
    _init_repo(tmp_path)
    monkeypatch.setattr(leases, "WRITE_LEASE_WAIT_S", 0.05)  # 租约路径不拖慢用例
    script = [
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "feature.py", "content": "print('hi')\n"})],
        [TextBlock(text="写好了")],
    ]
    tasks = TaskManager(provider_factory=lambda: FakeProvider(script), working_dir=tmp_path)
    tool = SpawnAgentTool(tasks)
    out = await tool.run(
        tool.args_model(agent_type="task", prompt="写个功能", background=True, isolated=True),
        ToolContext(working_dir=tmp_path, session_id="s1"),
    )
    task_id = out.split("task_id=")[1].split(";")[0]
    rec = await _wait_status(tasks, task_id, "done")
    assert rec is not None and rec.status == "done", rec.error if rec else "任务不存在"

    assert rec.isolated and rec.branch == f"skysheep/task-{rec.id}"
    wt = Path(rec.worktree_dir)
    assert wt.is_dir() and (wt / "feature.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert not (tmp_path / "feature.py").exists(), "主工作区不应被隔离任务触碰"
    assert rec.commit_id, "完成时应自动提交"
    r = subprocess.run(  # noqa: S603
        ["git", "branch", "--list", rec.branch], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert rec.branch in r.stdout

    report = await CheckTaskTool(tasks).run(
        CheckTaskArgs(task_id=task_id),
        ToolContext(working_dir=tmp_path, session_id="s1"),
    )
    assert "隔离工作区" in report and rec.branch in report and "git merge" in report

    # 租约枢纽按目录分键：隔离任务的写入不占主工作区的租约表
    assert leases.hub_for(wt) is not leases.hub_for(tmp_path)


async def test_done_payload_carries_merge_guidance(tmp_path):
    """完成载荷附合并/清理指引；未提交（异常收尾）时如实说明。"""
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="x")]]), working_dir=tmp_path,
    )
    rec = tasks._new_record("task", "干活", session_id="s1")
    rec.isolated = True
    rec.status = "done"
    rec.result = "报告"
    rec.worktree_dir = str(tmp_path / "wt")
    rec.branch = "skysheep/task-abc"
    rec.commit_id = "abc1234"
    payload = tasks.result_payload(rec)
    assert "git merge skysheep/task-abc" in payload and "abc1234" in payload

    rec.commit_id = ""
    assert "未自动提交" in tasks.result_payload(rec)
