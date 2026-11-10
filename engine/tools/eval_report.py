"""评测指标报表：把 evals/.metrics/last-run.jsonl 聚合成 Markdown / JSON。

用法：
    python tools/eval_report.py                      # 打印最近一次运行的 Markdown 摘要
    python tools/eval_report.py --json               # 机器可读（CI 产物/看板）
    python tools/eval_report.py --jsonl path/to.jsonl
    python tools/eval_report.py --fail-on-failed     # 有失败用例时退出码 1（CI 门禁用）

数据来源是 evals/conftest.py 的指标插件：跑 ``uv run pytest -m eval`` 后自动写
``evals/.metrics/last-run.jsonl``（``SKYSHEEP_EVAL_METRICS=0`` 可关闭）。

这个脚本只读文件、不跑测试、不联网；聚合口径见 evals/_metrics.py 的 aggregate。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_JSONL = ENGINE_DIR / "evals" / ".metrics" / "last-run.jsonl"


def _load(jsonl: Path) -> list[dict]:
    sys.path.insert(0, str(ENGINE_DIR))
    sys.path.insert(0, str(ENGINE_DIR / "src"))
    from evals._metrics import load_records  # noqa: PLC0415 - 先补 sys.path

    return load_records(jsonl)


def _render_markdown(summary: dict) -> str:
    rate = summary.get("pass_rate")
    rate_text = f"{rate * 100:.1f}%" if isinstance(rate, (int, float)) else "-"
    lines = [
        "# 评测指标摘要",
        "",
        f"- 用例 {summary['total']}：通过 {summary['passed']} · 失败 {summary['failed']}"
        f" · 跳过 {summary['skipped']} · 通过率 {rate_text}",
        f"- 耗时：p50 {summary['p50_ms']:.0f}ms · p95 {summary['p95_ms']:.0f}ms"
        f" · 最长 {summary['max_ms']:.0f}ms",
        f"- 成本：token 合计 {summary['total_tokens']:,} · 工具调用 {summary['tool_calls']}",
    ]
    buckets = summary.get("buckets") or {}
    if buckets:
        lines += ["", "| 场景文件 | 通过 | 失败 | 跳过 | p50 | p95 |", "|---|---|---|---|---|---|"]
        for name, b in buckets.items():
            lines.append(
                f"| {name} | {b['passed']} | {b['failed']} | {b['skipped']} "
                f"| {b['p50_ms']:.0f}ms | {b['p95_ms']:.0f}ms |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="聚合评测指标（evals/.metrics/last-run.jsonl）")
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL), help="指标记录文件（JSONL）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    ap.add_argument(
        "--fail-on-failed", action="store_true",
        help="存在失败用例时退出码 1（CI 门禁用；默认恒 0）",
    )
    args = ap.parse_args(argv)
    path = Path(args.jsonl)
    if not path.is_file():
        print(f"找不到指标文件：{path}\n先跑 `uv run pytest -m eval`（指标插件会写这个文件）")
        return 1
    records = _load(path)
    if not records:
        print(f"指标文件为空：{path}")
        return 1
    from evals._metrics import aggregate  # noqa: PLC0415

    summary = aggregate(records)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(_render_markdown(summary), end="")
    if args.fail_on_failed and summary["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
