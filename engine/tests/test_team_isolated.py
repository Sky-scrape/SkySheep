"""A1 团队隔离执行：执行型队员跑在各自 git worktree 分支，主工作区不受影响。

默认关；开启后每轮完成自动提交到 `skysheep/team-*` 分支并在频道提示合并
（引擎不自动 merge）。非 git 项目 / 未开启时退化为共享工作区（回归）。
"""

from __future__ import annotations

import subprocess

import pytest

from skysheep.core.team import TeamMemberSpec, TeamOrchestrator
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import PermissionGate, WhitelistRule
from skysheep.tools import ToolRegistry, WriteFileTool


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _init_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "user.email", "t@example.com")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")


def _mk_orch(repo, provider, *, isolate: bool):
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    orch = TeamOrchestrator(
        emit=emit,
        working_dir=repo,
        registry=ToolRegistry([WriteFileTool()]),
        # write_file 整工具白名单：非隔离路径的写入会走会话门，没有前端应答；
        # 白名单让测试聚焦「隔离是否生效」而不是再搭一套确认往返。
        gate=PermissionGate(
            working_dir=repo,
            session_rules=[WhitelistRule(tool="write_file", kind="always")],
        ),
        isolate_exec=isolate,
    )
    member = TeamMemberSpec(
        name="小写", provider_name="p", model="m", provider=provider,
    )
    return events, orch, member


def _writing_provider(filename: str):
    return FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": filename, "content": "from worktree"})],
        [TextBlock(text="写好了")],
    ])


@pytest.mark.asyncio
async def test_member_exec_runs_in_worktree_branch(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    events, orch, member = _mk_orch(repo, _writing_provider("wt.txt"), isolate=True)
    await orch.create_team([member])
    task = orch.board.add("写个文件", "小写", type="exec")
    orch.board.set_status(task.id, "in_progress")  # 执行型工单才挂工作区工具表
    res = await orch.run_member_turn("小写")

    assert res.status == "done", res.error
    # 主工作区看不到队友写的文件；改动在 worktree 里
    assert not (repo / "wt.txt").exists(), "隔离执行不得写进主工作区"
    wts = list((repo / ".skysheep" / "worktrees").glob("team-*"))
    assert wts and (wts[0] / "wt.txt").exists(), "改动应落在队员的 worktree 里"
    # 已自动提交到独立分支（不自动 merge）
    branches = _git(repo, "branch", "--list", "skysheep/team-*").stdout
    assert "skysheep/team-" in branches, branches
    log = _git(wts[0], "log", "--oneline", "-1").stdout
    assert "小写" in log or "team" in log
    # 频道里有合并提示（含分支名），主工作区 HEAD 未动
    notes = [m.text for m in orch.channel.messages
             if m.from_member == "system" and "隔离执行" in (m.text or "")]
    assert notes and "git merge skysheep/team-" in notes[0], notes
    head = _git(repo, "log", "--oneline", "-1").stdout
    assert "base" in head


@pytest.mark.asyncio
async def test_member_exec_without_isolation_writes_main_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    events, orch, member = _mk_orch(repo, _writing_provider("plain.txt"), isolate=False)
    await orch.create_team([member])
    task = orch.board.add("写个文件", "小写", type="exec")
    orch.board.set_status(task.id, "in_progress")  # 执行型工单才挂工作区工具表
    res = await orch.run_member_turn("小写")
    assert res.status == "done", res.error
    assert (repo / "plain.txt").exists(), "未开启隔离时应照旧写主工作区"
    assert not (repo / ".skysheep" / "worktrees").exists()


@pytest.mark.asyncio
async def test_isolation_falls_back_on_non_git_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    events, orch, member = _mk_orch(plain, _writing_provider("x.txt"), isolate=True)
    await orch.create_team([member])
    task = orch.board.add("写个文件", "小写", type="exec")
    orch.board.set_status(task.id, "in_progress")  # 执行型工单才挂工作区工具表
    res = await orch.run_member_turn("小写")
    assert res.status == "done", res.error
    # 不是 git 仓库：静默退化为共享工作区，不拦活
    assert (plain / "x.txt").exists()
    assert not (plain / ".skysheep" / "worktrees").exists()
