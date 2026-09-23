"""git worktree 隔离工作区：并行任务的目录级隔离（spawn_agent isolated=true）。

隔离派生把一个后台子任务放进项目自己的 git worktree：独立目录 + 独立分支，
写入不碰主工作区，任务完成时引擎把改动自动提交到该分支；审查与合并永远由
主会话在权限门的确认下完成——引擎不自动 merge。

只适用 git 仓库：非仓库项目在派生时即被拒绝（fail fast），不影响普通派生。
工作区放在 <项目>/.skysheep/worktrees/<task_id>（与报告同一条「引擎本地目录」
约定），并在仓库本地忽略（.git/info/exclude，不污染被跟踪的 .gitignore）里
登记该目录，git status 不显示。

安全边界：这里只做机械操作（建 worktree / 提交）。worktree 内的写入由
IsolatedGate 放行（工作区是引擎建的、分支可弃，隔离本身就是确认的替代），
命令执行仍然预拒绝——它能越出工作区，且后台任务没有确认通道。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# 大仓库的 worktree 检出可能超过一般命令的秒级预算
_GIT_TIMEOUT_S = 300


def is_git_repo(workdir: Path | None) -> bool:
    """是否具备隔离派生前提：有工作目录且带 .git（仓库或 worktree 均可）。"""
    return workdir is not None and (Path(workdir) / ".git").exists()


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - exe 固定为 git
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_S,
    )


def _exclude_worktrees(main_dir: Path) -> None:
    """把 /.skysheep/worktrees/ 追加进仓库本地忽略（.git/info/exclude）。

    本地忽略不进版本库，比改 .gitignore 干净；主仓库自身是 worktree（.git 是
    文件）时定位不到真实 git 目录，跳过（只影响 git status 观感，best-effort）。
    """
    git_path = main_dir / ".git"
    if not git_path.is_dir():
        return
    try:
        info = git_path / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        line = "/.skysheep/worktrees/"
        text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
        if line not in text:
            exclude.write_text(
                (text.rstrip("\n") + "\n" if text.strip() else "") + line + "\n",
                encoding="utf-8",
            )
    except OSError:
        pass


def ensure_worktree(main_dir: Path, wt_dir: Path, branch: str) -> None:
    """在 main_dir 仓库上建独立 worktree（分支 branch，检出在 wt_dir）。

    幂等：wt_dir 已是有效 worktree（重启恢复 / 重复派发）时直接复用；
    分支已存在但工作区缺失（上次中断残留）时按既有分支检出。
    失败抛 RuntimeError（带 git 的 stderr 摘要）。
    """
    main_dir = Path(main_dir)
    if not is_git_repo(main_dir):
        raise RuntimeError(f"{main_dir} 不是 git 仓库（未找到 .git）")
    wt_dir = Path(wt_dir)
    if (wt_dir / ".git").exists():
        return
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _exclude_worktrees(main_dir)
    r = _git(
        ["-c", "core.longpaths=true", "worktree", "add", "-b", branch, str(wt_dir), "HEAD"],
        cwd=main_dir,
    )
    if r.returncode != 0:
        r2 = _git(["worktree", "add", str(wt_dir), branch], cwd=main_dir)
        if r2.returncode != 0:
            err = (r2.stderr or r.stderr or r2.stdout or "git worktree add 失败").strip()
            raise RuntimeError(err[:300])


def remove_worktree(main_dir: Path, wt_dir: Path, branch: str) -> None:
    """移除隔离 worktree：删目录 + 清注册（best-effort，失败不抛给调用方）。

    取消的隔离任务会留下「目录 + worktree 注册」两样残留：主仓的
    ``git worktree list`` 一直挂着它，得手动 prune 才消失（安全审查 M15）。
    这里统一收拾：优先 ``worktree remove --force``（一次清掉目录与注册）；
    目录已被手动删掉时退化为 ``worktree prune``。

    分支保留（不删）：调用方通常先把取消前的部分改动提交到分支，删分支等于
    扔掉人做的活；不需要时由用户自行 ``git branch -D``（收尾说明里会提示）。
    """
    main_dir, wt_dir = Path(main_dir), Path(wt_dir)
    try:
        r = _git(["worktree", "remove", "--force", str(wt_dir)], cwd=main_dir)
        if r.returncode != 0:
            # 目录可能已不存在（手动删过/清理中断）：注册还在，prune 掉
            _git(["worktree", "prune"], cwd=main_dir)
    except Exception:  # noqa: BLE001 - 清理是尽力而为，不挡任务收尾
        pass


def commit_all(worktree: Path, message: str) -> str:
    """把 worktree 内全部改动提交到它的分支，返回短 commit id。

    无可提交改动返回空串；真实失败（含 git 身份未配置的首次失败会自动以
    占位身份重试一次）抛 RuntimeError，调用方按 best-effort 处理——提交失败
    不该把已完成的任务判成失败，改动都在工作区里。
    """
    wt = Path(worktree)
    _git(["add", "-A"], cwd=wt)
    r = _git(["commit", "--no-verify", "-m", message], cwd=wt)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "nothing to commit" in err or "nothing added to commit" in err:
            return ""
        r2 = _git(
            [
                "-c", "user.name=SkySheep", "-c", "user.email=skysheep@localhost",
                "-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", message,
            ],
            cwd=wt,
        )
        if r2.returncode != 0:
            raise RuntimeError((r2.stderr or err).strip()[:300])
        r = r2
    rid = _git(["rev-parse", "--short", "HEAD"], cwd=wt)
    return rid.stdout.strip() if rid.returncode == 0 else ""
