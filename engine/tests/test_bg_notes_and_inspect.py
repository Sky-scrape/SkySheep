"""后台进程「结束回灌」与运行巡检的测试。

两件事都是「跑完了怎么办」：前者让模型不用自己轮询 `action="read"`，
后者让用户知道跑完的那批里有没有跑歪的。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import FakeProvider

from skysheep.messages import TextBlock
from skysheep.server.backend_parts import run_inspect
from skysheep.tools import shell as shell_mod

# ---------- 后台进程结束提示 ----------


def _reset_bg_state():
    """清进程级注册表，避免用例之间互相看到对方起的进程。"""
    for bid in list(shell_mod._BG):
        shell_mod._BG.pop(bid, None)
    shell_mod._BG_NOTES.clear()


async def test_bg_process_records_completion_note(home):
    """进程退出后写一条提示，宿主下一轮注入（以前得靠模型自己轮询）。"""
    _reset_bg_state()
    info = shell_mod._bg_start(
        shell_mod._shell_argv("echo done-marker"),
        str(home), owner="sess-a",
    )
    note = ""
    for _ in range(100):  # 最多等 5 秒：Windows 起 cmd 有几十毫秒开销
        note = shell_mod.pop_bg_notes("sess-a")
        if note:
            break
        await asyncio.sleep(0.05)
    assert note, "进程退出后应留下一条完成提示"
    assert "done-marker" in note
    assert str(info["pid"]) in note
    # 取走即清空：同一条提示不会被两轮重复注入
    assert shell_mod.pop_bg_notes("sess-a") == ""


async def test_bg_notes_are_per_session(home):
    """别的会话取不到我起的进程提示（并行会话之间不能互相串台）。"""
    _reset_bg_state()
    shell_mod._bg_start(shell_mod._shell_argv("echo x"), str(home), owner="sess-b")
    for _ in range(100):
        if shell_mod.pop_bg_notes("sess-b"):
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - 只在严重超时时触发
        pytest.fail("提示没等到")
    assert shell_mod.pop_bg_notes("sess-other") == ""


async def test_bg_kill_does_not_record_note(home):
    """主动 kill 的不发提示：这次终止是模型自己发起的，再塞一条纯属白耗上下文。"""
    _reset_bg_state()
    info = shell_mod._bg_start(
        shell_mod._shell_argv("ping -n 30 127.0.0.1 >nul" if shell_mod.IS_WINDOWS
                              else "sleep 30"),
        str(home), owner="sess-c",
    )
    shell_mod._bg_kill(int(info["pid"]), sid="sess-c")
    await asyncio.sleep(0.5)
    assert shell_mod.pop_bg_notes("sess-c") == ""


# ---------- 运行巡检 ----------


async def _seed_runs(store, count: int, *, status="ok", error_kind=""):
    for i in range(count):
        run = await store.add_automation_run(
            kind="cron", task_id=i + 1, project_id=None, trigger="schedule",
        )
        await store.finish_automation_run(
            int(run["id"]), status=status, duration_s=60.0,
            result_excerpt=f"第{i}次运行", error_kind=error_kind,
            outcome="ok" if status == "ok" else "failed",
        )


def test_build_digest_is_one_line_per_run():
    runs = [{
        "kind": "cron", "task_id": 7, "status": "ok", "outcome": "ok",
        "error_kind": "", "denied_count": 2, "duration_s": 90.0,
        "in_tokens": 1000, "out_tokens": 200, "result_excerpt": "改了三个文件\n第二行",
    }]
    line = run_inspect.build_digest(runs)
    assert line.startswith("cron#7 | ok | ok")
    assert "2" in line and "1.5min" in line
    assert "\n" not in line, "每条运行压成一行，模型按行读"

    long_line = run_inspect.build_digest([
        {**runs[0], "result_excerpt": "很长的产出" * 40}
    ])
    assert len(long_line) < 200, "产出摘要要截断，不能让一张表爆窗口"


async def test_inspect_skips_when_too_few_runs(home, store):
    await _seed_runs(store, run_inspect.MIN_RUNS - 1)
    provider = FakeProvider([[TextBlock(text="无需留意")]])
    res = await run_inspect.inspect_runs(store, provider)
    assert res["ok"] is False and res["reason"] == "too_few_runs"


async def test_inspect_writes_report_and_state(home, store):
    await _seed_runs(store, 4)
    provider = FakeProvider([[TextBlock(text="- cron#1 | 反复失败 | 看一下脚本")]])
    res = await run_inspect.inspect_runs(store, provider, hours=24)
    assert res["ok"] is True
    assert "反复失败" in res["text"]
    report = run_inspect.Path(res["report"])
    assert report.is_file()
    body = report.read_text(encoding="utf-8")
    assert "cron#1" in body and "原始记录" in body
    state = run_inspect.load_state()
    assert state["last_at"] > 0 and state["last_summary"]


async def test_pass_respects_switch_and_interval(home, store):
    await _seed_runs(store, 4)
    # 三次调用各要一段脚本：FakeProvider 用尽后抛错，会把「该跑」误判成 provider_error
    provider = FakeProvider([[TextBlock(text="无需留意")] for _ in range(3)])

    class _Backend:
        pass

    be = _Backend()
    be.store = store
    be.provider = provider

    assert (await run_inspect.run_inspect_pass(be))["reason"] == "disabled"

    state = run_inspect.load_state()
    state["enabled"] = True
    run_inspect.save_state(state)
    first = await run_inspect.run_inspect_pass(be)
    assert first["ran"] is True
    # 间隔没到：不重复烧模型调用
    second = await run_inspect.run_inspect_pass(be)
    assert second["ran"] is False and second["reason"] == "not_due"
    # 越过间隔（用 now 直接推时间）就能再跑
    later = time.time() + (state["interval_hours"] + 1) * 3600
    third = await run_inspect.run_inspect_pass(be, now=later)
    assert third["ran"] is True


async def test_pass_does_not_advance_clock_when_nothing_to_judge(home, store):
    """样本不够时不消耗 last_at：攒够了下一次还能跑（否则要白等一个间隔）。"""
    await _seed_runs(store, 2)

    class _Backend:
        pass

    be = _Backend()
    be.store = store
    be.provider = FakeProvider([[TextBlock(text="无需留意")]])
    state = run_inspect.load_state()
    state["enabled"] = True
    run_inspect.save_state(state)

    res = await run_inspect.run_inspect_pass(be)
    assert res["ran"] is False
    assert run_inspect.load_state()["last_at"] == 0.0
