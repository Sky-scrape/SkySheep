"""Windows 系统计划任务真机验证（schtasks 实际执行）。

进程内单测（test_system_schedule.py）用假 subprocess 覆盖了参数映射与回写；
本文件用**真 schtasks** 跑一遍注册 → 查询 → 注销闭环，抓住模拟层抓不住的
问题：schtasks 拒收的参数组合、任务名冲突、输出格式随系统语言的变化。

测试任务用独立前缀 SKYSHEEPETEST-* 注册（不与产品的 SkySheepCron-* 混淆），
结束后无论成败都注销清理。仅在 Windows 且 schtasks 可用时运行。
标记 ``slow + e2e``：真系统子进程交互，不占每提交的单测 CI；CI 的 e2e
专层（windows-latest）会实际执行全链路。
"""

from __future__ import annotations

import subprocess

import pytest

from skysheep.core import system_schedule as sched

TEST_PREFIX = "\\SKYSHEEPETEST-"


def _schtasks_available() -> bool:
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.slow,
    pytest.mark.e2e,
    pytest.mark.skipif(not _schtasks_available(), reason="schtasks 不可用"),
]


def _register_raw(task_id: int, cron_spec: dict, project_dir: str) -> tuple[bool, str]:
    """按产品参数映射注册，但任务名落在隔离前缀下（不改产品代码）。"""
    import sys

    from skysheep.core.system_schedule import build_schtasks_args
    args = build_schtasks_args(task_id, cron_spec, project_dir, sys.executable)
    # 把产品前缀换成测试前缀：/TN 紧跟在 /F 之后
    i = args.index("/TN")
    args[i + 1] = TEST_PREFIX + str(task_id)
    return sched._run_schtasks(args)


def _exists(task_id: int) -> bool:
    ok, out = sched._run_schtasks(["/Query", "/FO", "CSV", "/NH", "/TN", TEST_PREFIX + str(task_id)])
    return ok and TEST_PREFIX + str(task_id) in out


@pytest.fixture
def task_ids():
    """注册过的测试任务 id；用例结束统一注销。"""
    ids: list[int] = []
    yield ids
    for tid in ids:
        sched._run_schtasks(["/Delete", "/TN", TEST_PREFIX + str(tid), "/F"])


def test_real_register_query_unregister(task_ids):
    """真机闭环：注册成功 → 系统里查得到 → 注销后消失。"""
    tid = 987001
    task_ids.append(tid)
    ok, out = _register_raw(
        tid,
        {"schedule_type": "daily", "time_of_day": "09:00"},
        "C:/nonexistent-proj",  # /TR 只是字符串，注册不校验目录存在
    )
    assert ok, f"schtasks /Create 失败：{out}"
    assert _exists(tid), "注册后系统里应能查到该任务"

    ok, out = sched._run_schtasks(["/Delete", "/TN", TEST_PREFIX + str(tid), "/F"])
    assert ok, f"schtasks /Delete 失败：{out}"
    assert not _exists(tid), "注销后任务应消失"
    task_ids.remove(tid)


def test_real_register_weekly_with_weekday(task_ids):
    """weekly + weekday 的 /SC WEEKLY /D MON 组合被真机接受。"""
    tid = 987002
    task_ids.append(tid)
    ok, out = _register_raw(
        tid,
        {"schedule_type": "weekly", "weekday": 0, "time_of_day": "10:30"},
        "C:/nonexistent-proj",
    )
    assert ok, f"weekly 注册失败：{out}"
    assert _exists(tid)


def test_real_register_interval_minutes(task_ids):
    """interval 5 分钟 → /SC MINUTE /MO 5 被真机接受。"""
    tid = 987003
    task_ids.append(tid)
    ok, out = _register_raw(
        tid,
        {"schedule_type": "interval", "interval_minutes": 5},
        "C:/nonexistent-proj",
    )
    assert ok, f"interval 注册失败：{out}"
    assert _exists(tid)


def test_real_reregister_overwrites(task_ids):
    """/F 覆盖：同一任务名重复注册不报错（更新任务的重建路径）。"""
    tid = 987004
    task_ids.append(tid)
    spec = {"schedule_type": "daily", "time_of_day": "08:00"}
    ok1, out1 = _register_raw(tid, spec, "C:/nonexistent-proj")
    ok2, out2 = _register_raw(tid, {**spec, "time_of_day": "11:00"}, "C:/nonexistent-proj")
    assert ok1 and ok2, f"覆盖注册失败：{out1} / {out2}"
    assert _exists(tid)


def test_real_unregister_missing_task_is_tolerable():
    """注销不存在的任务：schtasks 返回非零但不抛异常（对账语义容忍）。"""
    ok, out = sched._run_schtasks(["/Delete", "/TN", TEST_PREFIX + "987999", "/F"])
    assert isinstance(ok, bool) and isinstance(out, str)
