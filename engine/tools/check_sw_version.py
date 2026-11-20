"""SW_VERSION 提示级对账——前端壳文件变了、Service Worker 版本号没跟着升时提醒。

背景（2026-11-18 返工复盘 / 2026-11-20 裁决）：PWA 壳的失效靠 ``SW_VERSION``
递增（缓存名带版本号，activate 时删旧缓存）；漏递增时 stale-while-revalidate
还能在下一次打开拉到新壳，但用户会先吃一次旧文件。复盘已否决「内容指纹自动
校验」的硬门禁方案（每次前端改动都多一步机械更新，收益/摩擦比不划算），
所以这里从轻：只做提示级对账，默认退出 0，``release.py`` 在打包前调用它打印
警告；``--strict`` 留给手动检查。

判定只看两件事：

1. ``git diff --name-only <base>..HEAD -- engine/src/skysheep/server/static/``
   是否有改动（壳目录内任何文件，含 ``sw.js`` 自己）；
2. ``<base>:engine/src/skysheep/server/static/sw.js`` 与工作区 ``sw.js`` 的
   ``SW_VERSION`` 声明是否相同。

没有壳改动、或版本号已变 → 退出 0；壳改了但版本没变 → 默认警告退出 0，
``--strict`` 退出 1；对账本身做不下去（git 失败 / 解析不出）→ 默认警告退出 0，
``--strict`` 退出 2。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 脚本位于 <repo>/engine/tools/：parents[0]=tools、parents[1]=engine、parents[2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATIC_REL = "engine/src/skysheep/server/static"
_SW_REL = f"{_STATIC_REL}/sw.js"
_SW_VERSION_RE = re.compile(r"""SW_VERSION\s*=\s*["']([^"']+)["']""")


def _git(*args: str, cwd: Path = _REPO_ROOT) -> str:
    """跑 git 并返回 stdout；失败抛 RuntimeError（调用方按「无法对账」处理）。"""
    cmd = ["git", "-c", "core.quotepath=false", *args]
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


def parse_sw_version(text: str) -> str | None:
    """从 sw.js 源码里取 ``SW_VERSION`` 的字面值；没有返回 None。"""
    m = _SW_VERSION_RE.search(text)
    return m.group(1) if m else None


def shell_files_changed(diff_output: str) -> bool:
    """``git diff --name-only`` 的输出里是否有壳目录内的文件。"""
    return any(
        line.strip().startswith(_STATIC_REL + "/")
        for line in diff_output.splitlines()
        if line.strip()
    )


def check(base: str, cwd: Path = _REPO_ROOT) -> tuple[int, list[str]]:
    """对账 ``base..HEAD``；返回 ``(0=通过/跳过, 1=壳变了版本没变, 2=无法对账)``。"""
    try:
        diff = _git(
            "diff", "--name-only", f"{base}..HEAD", "--", _STATIC_REL, cwd=cwd
        )
    except RuntimeError as exc:
        return 2, [f"⚠ 无法完成 SW_VERSION 对账：{exc}"]
    if not shell_files_changed(diff):
        return 0, [f"✓ {base}..HEAD 没有前端壳文件改动，跳过 SW_VERSION 对账"]

    try:
        current_text = (cwd / _SW_REL).read_text(encoding="utf-8")
    except OSError as exc:
        return 2, [f"⚠ 读不了工作区 {_SW_REL}：{exc}"]
    except UnicodeDecodeError as exc:
        return 2, [f"⚠ 工作区 {_SW_REL} 不是合法 UTF-8：{exc}"]
    current = parse_sw_version(current_text)
    if current is None:
        return 2, [f"⚠ 工作区 {_SW_REL} 里解析不出 SW_VERSION"]

    try:
        previous_text = _git("show", f"{base}:{_SW_REL}", cwd=cwd)
    except RuntimeError as exc:
        return 2, [f"⚠ 读不了 {base} 的 {_SW_REL}：{exc}"]
    previous = parse_sw_version(previous_text)
    if previous is None:
        return 2, [f"⚠ {base} 的 {_SW_REL} 里解析不出 SW_VERSION，无法对账"]

    if current == previous:
        return 1, [
            f"⚠ {base}..HEAD 有前端壳文件改动，但 SW_VERSION 没变（{current}）。",
            "  用户下次打开会先吃到旧缓存（stale-while-revalidate 会在后台补新），",
            "  正常流程应递增 SW_VERSION。这是提示，不拦发布。",
        ]
    return 0, [f"✓ SW_VERSION 已从 {previous} 升到 {current}"]


def _force_utf8_stdio() -> None:
    """与 check_versions.py 同款：CP1252 管道下不因中文/符号崩掉。"""
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
        prog="check_sw_version.py",
        description="提示级对账：前端壳文件变了但 SW_VERSION 没变时提醒（默认不拦）",
    )
    parser.add_argument(
        "--base", required=True, help="对账基准 ref（通常是上一发布 tag，如 v2.7.1）",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="壳文件变了但 SW_VERSION 没变时退出 1；默认只打印警告退出 0",
    )
    parser.add_argument(
        "--cwd", type=Path, default=_REPO_ROOT, help="仓库根（默认按脚本位置推导）",
    )
    args = parser.parse_args(argv)
    code, lines = check(args.base, args.cwd)
    print("\n".join(lines))
    if code == 0:
        return 0
    if code == 1 and args.strict:
        return 1
    if code == 2 and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
