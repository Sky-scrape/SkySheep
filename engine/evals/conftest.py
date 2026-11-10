"""评测基线夹具（engine/evals/）。

与 tests/conftest.py 平行：evals 不在 tests/ 目录树内，本文件以
``evals.conftest`` 导入（evals 是包，见 __init__.py），两边互不干扰。
驱动 Agent 循环的辅助函数在 evals/_harness.py（conftest 不能被测试文件
按顶层名安全导入——顶层名 conftest 属于 tests/conftest.py）。

另外承载**评测指标插件**：每隔一条用例记录结果与耗时，场景可通过 ``metrics``
夹具附上 ``TurnMetrics``（耗时/token/工具次数/确认次数），会话结束写
``evals/.metrics/last-run.json(l)`` 并打印一行汇总。环境变量
``SKYSHEEP_EVAL_METRICS=0`` 关闭；``SKYSHEEP_EVAL_METRICS_DIR`` 改输出目录。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

_METRICS_ENV = "SKYSHEEP_EVAL_METRICS"
_METRICS_DIR_ENV = "SKYSHEEP_EVAL_METRICS_DIR"
_RECORDS: list[dict] = []
_SESSION_START = time.monotonic()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """隔离的 SkySheep home + 临时项目目录（proj/）——与 tests/conftest.py 同款。

    SKYSHEEP_HOME 指向临时目录：memory_write / 任务簿持久化 / 会话库等一切
    引擎主目录读写都落在 tmp，绝不触碰真实 ~/.skysheep。
    """
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    (tmp_path / "proj").mkdir()
    return tmp_path


@pytest.fixture
def metrics():
    """场景指标收集器：``metrics.append(TurnMetrics.from_events(events))``。

    随用例结果一起写进 last-run.jsonl（``record.extra``），供聚合报告使用；
    不用这个夹具的场景照样有耗时记录，只是没有 token/工具次数明细。
    """
    return []


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """本目录收集到的用例自动打 eval 标记：新场景文件漏写 pytestmark 也不会漏跑。"""
    root = Path(__file__).resolve().parent
    for item in items:
        if Path(str(item.fspath)).is_relative_to(root):
            item.add_marker(pytest.mark.eval)


def _metrics_enabled() -> bool:
    return os.environ.get(_METRICS_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _metrics_dir() -> Path:
    raw = os.environ.get(_METRICS_DIR_ENV, "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parent / ".metrics"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """每条用例结束后收一条记录：结果、耗时、场景附带的 TurnMetrics。"""
    outcome = yield
    report = outcome.get_result()
    if not _metrics_enabled():
        return
    interesting = report.when == "call" or (
        report.when == "setup" and report.outcome in ("failed", "skipped")
    )
    if not interesting:
        return
    # 只记评测目录的用例：全量 pytest 会把 tests/ 也收进来，混进最后一份
    # eval_report 报表会让「评测通过率」失真。
    node = report.nodeid.replace("\\", "/")
    if not node.startswith("evals/"):
        return
    extra: dict = {}
    holder = getattr(item, "funcargs", {}).get("metrics") if hasattr(item, "funcargs") else None
    if isinstance(holder, list) and holder:
        merged: dict = {}
        for m in holder:
            d = m.as_dict() if hasattr(m, "as_dict") else dict(m)
            for k, v in d.items():
                if isinstance(v, (int, float)) and isinstance(merged.get(k), (int, float)):
                    merged[k] += v
                else:
                    merged[k] = v
        extra = merged
    _RECORDS.append({
        "id": report.nodeid,
        "outcome": report.outcome,
        "wall_ms": int(report.duration * 1000),
        "extra": extra,
    })


def pytest_sessionfinish(session, exitstatus: int) -> None:
    """会话结束落盘 + 打印一行汇总（写盘失败只警告，绝不改变退出码）。"""
    if not _metrics_enabled() or not _RECORDS:
        return
    try:
        from evals._metrics import aggregate, write_records

        d = _metrics_dir()
        d.mkdir(parents=True, exist_ok=True)
        write_records(d / "last-run.jsonl", _RECORDS)
        agg = aggregate(_RECORDS)
        import json

        (d / "last-run.json").write_text(
            json.dumps(
                {"generated_at": time.time(),
                 "session_seconds": round(time.monotonic() - _SESSION_START, 1),
                 "summary": agg},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        rate = agg["pass_rate"]
        rate_text = f"{rate * 100:.0f}%" if isinstance(rate, (int, float)) else "-"
        print(
            f"\n[eval-metrics] {agg['total']} 项 · 通过 {agg['passed']} · 失败 "
            f"{agg['failed']} · 跳过 {agg['skipped']} · 通过率 {rate_text} · "
            f"p50 {agg['p50_ms']:.0f}ms · p95 {agg['p95_ms']:.0f}ms → {d}"
        )
    except Exception as e:  # noqa: BLE001 - 指标层不得影响测试结果
        print(f"\n[eval-metrics] 指标落盘失败（不影响测试结果）：{e}")
