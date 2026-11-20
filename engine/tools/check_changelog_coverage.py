"""发布预检：逐条确认 ``v<上一版>..HEAD`` 的 feat/fix/perf 都进过 CHANGELOG。

背景（2026-11-18 返工复盘）：CHANGELOG 漏记不是一次两次——

- ``1582b7d``（接力轮间隙误杀）在 2.6.6 定版改名后才发现；
- ``866d016``（协作四处回归）在 2.7.0 定版前才补；
- 2.6.5 打 tag 后才发现漏两条，只能 ``git tag -f`` 移 tag 再 push。

漏记本身不可怕，可怕的是发现得太晚：tag 已经推出去、Release 已经建好，
再补就要移 tag / 重发。这个脚本把「定版前逐个过提交」变成一条命令。
默认只报告不拦（后续提交补记是合法的）；``--strict`` 才在仍有缺失时退出 1。

历史遗留、确认已由后续提交补记的提交登记在同目录 ``changelog_coverage_ack.txt``
（一行一个 sha 前缀，``#`` 后写理由）。被 ack 的提交在报告里标 ``[ack]``，
不计入 ``--strict`` 的失败；``--no-ack`` 可临时忽略清单。

Git 输出一律按 UTF-8 读：仓库提交信息是中文，Windows 控制台默认编码会乱码。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 脚本位于 <repo>/engine/tools/：parents[0]=tools、parents[1]=engine、parents[2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TAG_RE = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_RELEASE_SUBJECT_RE = re.compile(r"^(feat|fix|perf)(\([^)]*\))?[:：]")
# ack 清单默认与脚本同目录（一行一个 sha 前缀；``#`` 后的注记不参与匹配）
_DEFAULT_ACK_PATH = Path(__file__).resolve().parent / "changelog_coverage_ack.txt"


@dataclass(frozen=True)
class Commit:
    sha: str
    date: str
    subject: str
    touched_changelog: bool
    acked: bool = False


def _git(*args: str, cwd: Path = _REPO_ROOT) -> str:
    """跑 git 并返回 stdout；失败抛 RuntimeError（调用方按预检失败处理）。"""
    cmd = [
        "git",
        "-c", "core.quotepath=false",
        "-c", "i18n.logOutputEncoding=UTF-8",
        *args,
    ]
    proc = subprocess.run(  # noqa: S603 - 参数固定，无 shell
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（退出码 {proc.returncode}）：{proc.stderr.strip()}"
        )
    return proc.stdout


def _pyproject_version(cwd: Path = _REPO_ROOT) -> str | None:
    path = cwd / "engine" / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def previous_tag(current_version: str | None = None, cwd: Path = _REPO_ROOT) -> str | None:
    """取最近的发布 tag；排除已经打在 HEAD 的当前版本 tag。"""
    tags = [
        tag for tag in _git("tag", "--list", "v*", "--sort=-v:refname", cwd=cwd).split()
        if _TAG_RE.match(tag)
    ]
    if current_version:
        tags = [tag for tag in tags if tag[1:] != current_version]
    return tags[0] if tags else None


def is_shallow_repository(cwd: Path = _REPO_ROOT) -> bool:
    """当前检出是否为浅克隆（CI checkout 默认 depth=1）。

    浅克隆里 ``git tag --list`` 可能取不到发布 tag，``git log v<上一版>..HEAD``
    也只看得见被取回的那几个提交——预检会退化成「只查 HEAD」：tag 构建空过，
    手动 dispatch 又可能把一条漏记的 feat/fix 误判成候选构建失败。历史不全时
    宁可显式失败，让调用方补全，也不给一个看起来通过的绿灯。
    """
    return _git("rev-parse", "--is-shallow-repository", cwd=cwd).strip() == "true"


def is_release_subject(subject: str) -> bool:
    """feat/fix/perf 才要求 CHANGELOG 覆盖；docs/chore/test 不强制。"""
    return bool(_RELEASE_SUBJECT_RE.match(subject))


def load_ack(path: Path) -> list[str]:
    """读 ack 清单：一行一个 sha 前缀，空行与 ``#`` 起头的行忽略，行尾注记剥掉。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"读不了 ack 清单（{path}）：{exc}") from exc
    prefixes: list[str] = []
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if body:
            prefixes.append(body)
    return prefixes


def is_acked(sha: str, prefixes: list[str]) -> bool:
    """按前缀匹配：``commit.sha.startswith(prefix)``。"""
    return any(sha.startswith(prefix) for prefix in prefixes)


def parse_log(out: str) -> list[tuple[str, str, str]]:
    """解析 ``git log --pretty=format:%H%x09%ad%x09%s`` 的输出。"""
    rows: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def _touched_changelog(sha: str, cwd: Path = _REPO_ROOT) -> bool:
    out = _git(
        "show", "--pretty=format:", "--name-only", sha, "--", "CHANGELOG.md",
        cwd=cwd,
    )
    return bool(out.strip())


def collect(
    tag: str | None,
    cwd: Path = _REPO_ROOT,
    ack_prefixes: list[str] | None = None,
) -> tuple[int, list[Commit]]:
    """返回 ``(区间内提交总数, feat/fix/perf 提交列表)``；ack 前缀同时打标。"""
    rev = f"{tag}..HEAD" if tag else "HEAD"
    out = _git(
        "log", rev, "--date=short", "--pretty=format:%H%x09%ad%x09%s",
        cwd=cwd,
    )
    rows = parse_log(out)
    prefixes = ack_prefixes or []
    relevant = [
        Commit(
            sha, date, subject, _touched_changelog(sha, cwd), is_acked(sha, prefixes)
        )
        for sha, date, subject in rows
        if is_release_subject(subject)
    ]
    return len(rows), relevant


def format_report(
    tag: str | None,
    total: int,
    commits: list[Commit],
    show_all: bool = False,
) -> str:
    """生成人读报告；``show_all`` 时逐条列，否则只列未触碰 CHANGELOG 的。"""
    missing = [c for c in commits if not c.touched_changelog and not c.acked]
    acked = [c for c in commits if not c.touched_changelog and c.acked]
    lines = [
        f"上一发布 tag：{tag or '（无 vX.Y.Z 标签，按全部历史）'}",
        f"区间：{tag + '..HEAD' if tag else 'HEAD'}，共 {total} 个提交，"
        f"其中 feat/fix/perf {len(commits)} 个",
    ]
    if not missing:
        if acked:
            lines.append(f"✓ 没有未处理的缺失提交（{len(acked)} 个历史提交已按 ack 清单豁免）：")
            if not show_all:
                for c in acked:
                    lines.append(f"  [ack] {c.sha[:9]} {c.date} {c.subject}")
        else:
            lines.append("✓ 全部 feat/fix/perf 提交都触碰过 CHANGELOG.md")
        if show_all:
            for c in commits:
                mark = "✓" if c.touched_changelog else "[ack]"
                lines.append(f"  {mark} {c.sha[:9]} {c.date} {c.subject}")
        return "\n".join(lines)

    lines.append(
        f"✗ {len(missing)} 个 feat/fix/perf 提交没有在本提交里改 CHANGELOG.md"
        "（可能由后续提交补记，请逐条对照定版段落确认）："
    )
    if acked:
        lines.append("  （[ack] 为已按 ack 清单豁免的历史提交，不计入失败）")
    shown = commits if show_all else [c for c in commits if not c.touched_changelog]
    for c in shown:
        if c.touched_changelog:
            mark = "✓"
        elif c.acked:
            mark = "[ack]"
        else:
            mark = "✗"
        lines.append(f"  {mark} {c.sha[:9]} {c.date} {c.subject}")
    if not show_all:
        lines.append("  （加 --all 查看全部 feat/fix/perf 提交）")
    return "\n".join(lines)


def _force_utf8_stdio() -> None:
    """与 check_versions.py 同款：CP1252 管道下不因 ✓/✗ 崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="check_changelog_coverage.py",
        description="发布预检：列出 v<上一版>..HEAD 中未触碰 CHANGELOG.md 的 feat/fix/perf 提交",
    )
    parser.add_argument(
        "--current-version",
        help="当前待发布版本（用于排除已打在 HEAD 的同名 tag；默认从 engine/pyproject.toml 读）",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="仍有未触碰 CHANGELOG 的提交时退出码 1（默认只报告）",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="列出全部 feat/fix/perf 提交（默认只列缺失与 ack 豁免的提交）",
    )
    parser.add_argument(
        "--ack", type=Path, default=None,
        help="ack 清单路径（默认脚本同目录 changelog_coverage_ack.txt）",
    )
    parser.add_argument(
        "--no-ack", action="store_true",
        help="忽略 ack 清单（所有未触碰 CHANGELOG 的提交都算缺失）",
    )
    parser.add_argument(
        "--cwd", type=Path, default=_REPO_ROOT, help="仓库根（默认按脚本位置推导）",
    )
    args = parser.parse_args(argv)
    ack_prefixes: list[str] = []
    if not args.no_ack:
        ack_path = args.ack or _DEFAULT_ACK_PATH
        if ack_path.is_file():
            try:
                ack_prefixes = load_ack(ack_path)
            except RuntimeError as exc:
                print(f"✗ {exc}", file=sys.stderr)
                return 2
        elif args.ack is not None:
            # 显式指定的路径写错要立刻报出来，不能默默当成「没有 ack」放行
            print(f"✗ ack 清单不存在：{ack_path}", file=sys.stderr)
            return 2
        else:
            print(f"提示：默认 ack 清单不存在（{ack_path}），按无 ack 处理")
    try:
        if is_shallow_repository(args.cwd):
            print(
                "✗ 当前仓库是浅克隆：拿不到完整的 v<上一版>..HEAD 历史与 tag，"
                "CHANGELOG 覆盖预检无法可信执行。\n"
                "  CI 请在 checkout 设置 fetch-depth: 0（并 fetch-tags: true）；"
                "本地可 git fetch --unshallow --tags 后重跑。",
                file=sys.stderr,
            )
            return 2
        current = args.current_version or _pyproject_version(args.cwd)
        tag = previous_tag(current, args.cwd)
        total, commits = collect(tag, args.cwd, ack_prefixes)
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    print(format_report(tag, total, commits, args.all))
    if args.strict and any(not c.touched_changelog and not c.acked for c in commits):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
