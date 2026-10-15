"""报告目录的滚动清理（team 频道报告 / 子代理报告共用）。

两处报告落盘目录（<工作区>/.skysheep/reports/）曾各有一份逐行相同的清理实现
（core/team.py 与 core/subagent.py，审查 C3）：都按「文件数或总字节超配额时按
mtime 从旧到新删」策略滚动，只是各自动各自的命名子集（team-*.md 与
<10位hex>-<类型>.md）、各有各的配额常量。提取到此处共用，避免口径分叉。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def prune_report_files(
    reports_dir: Path,
    *,
    name_ok: Callable[[str], bool],
    keep_files: int,
    keep_total_bytes: int,
) -> None:
    """报告目录滚动清理：文件数或总字节超配额时按 mtime 从旧到新删。

    只删 name_ok(文件名) 为真的文件（各调用方传入自己的命名形状，互不波及）。
    best-effort——探测/删除失败只放弃本轮清理或留下该文件，不阻断落盘。
    """
    try:
        stats = sorted(
            ((p, p.stat()) for p in reports_dir.glob("*.md") if p.is_file() and name_ok(p.name)),
            key=lambda kv: kv[1].st_mtime,
        )
    except OSError:
        return
    total = sum(st.st_size for _, st in stats)
    remaining = len(stats)
    for path, st in stats:
        if remaining <= keep_files and total <= keep_total_bytes:
            break
        try:
            path.unlink()
        except OSError:
            pass  # 删不掉（被占用等）：留下，下轮落盘时再试
        total -= st.st_size
        remaining -= 1
