"""SEC-M17：定时任务到点槽位的库级 CAS 认领（跨进程去重）。

背景：[cron] system_schedule=true 时，同一到点任务会被应用内 _cron_pass
（20 秒扫描）与 Windows 计划任务拉起的 cron-run 子进程双触发并发执行——
进程内 running 字典管不住跨进程，两进程各建会话跑同一 prompt（副作用重复、
token 双倍），update_cron_task 无条件 UPDATE 互覆账本。

修复：执行前库级 CAS 认领——UPDATE cron_tasks SET next_run_at=<占位>
WHERE id=? AND next_run_at=<旧值>，0 行受影响即放弃。本文件锁三件事：
认领语句本身的原子性（同值竞争恰有一方成功）、两个触发方经
run_cron_task_core 恰好执行一次（账本一行）、单触发源与 force 语义不变。
绝不触碰真实 ~/.skysheep，也不注册 schtasks（本文件不进 system_schedule）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from skysheep.messages import Message, TextBlock
from skysheep.server.backend_parts.automation import run_cron_task_core
from skysheep.session.store import SessionStore


def _runtime(text: str = "完成"):
    """run_cron_task_core 的 execute 返回值：带一条助手消息的最小 runtime。"""
    return SimpleNamespace(agent=SimpleNamespace(history=[
        Message.assistant([TextBlock(text=text)])])), "sid", False


async def _add_due_task(store, name: str, *, interval: int = 30,
                        due_offset: float = -5.0) -> dict:
    """建一个启用且已到点的 interval 任务（next_run_at 指向过去）。"""
    project = await store.get_or_create_project(f"/tmp/cron-claim-{name}")
    t = await store.add_cron_task(project.id, name, "干活", "interval",
                                  interval_minutes=interval)
    await store.update_cron_task(t["id"], next_run_at=time.time() + due_offset)
    return await store.get_cron_task(t["id"])


# ---- 认领语句本身的 CAS 语义 ----


async def test_claim_cas_same_expected_exactly_one_wins(tmp_path):
    """两个独立连接拿同一旧值竞争认领：恰有一方 True，落库值 = 胜方占位值。

    对应真实双触发：应用内扫描与 cron-run 子进程各读到同一个到点旧值，
    单条 UPDATE 的 WHERE 旧值条件保证只有先到者改得动（SQLite 写锁 +
    rowcount 判定），后到者 0 行受影响必须放弃。
    """
    db = tmp_path / "cas.db"
    s1 = await SessionStore(db).connect()
    s2 = await SessionStore(db).connect()
    try:
        t = await _add_due_task(s1, "同值竞争")
        stale = await s1.get_cron_task(t["id"])
        old = float(stale["next_run_at"])

        p1 = s1.compute_next_run({**stale, "last_run_at": time.time()})
        p2 = s2.compute_next_run({**stale, "last_run_at": time.time()})

        results = await asyncio.gather(
            s1.claim_cron_task(t["id"], old, p1),
            s2.claim_cron_task(t["id"], old, p2),
        )
        assert sorted(results) == [False, True], "同值竞争必须恰有一方认领成功"

        row = await s1.get_cron_task(t["id"])
        assert row["next_run_at"] == (p1 if results[0] else p2), \
            "落库的是胜方的占位值"
        assert row["next_run_at"] > time.time(), "占位值必须在未来（挡住后来者）"
    finally:
        await s1.close()
        await s2.close()


async def test_claim_cas_stale_expected_loses(store):
    """旧值已被改写（对方先认领）：后到者 CAS 落败，next_run_at 不被覆盖。"""
    t = await _add_due_task(store, "后到者落败")
    old = float(t["next_run_at"])
    placeholder = store.compute_next_run({**t, "last_run_at": time.time()})

    assert await store.claim_cron_task(t["id"], old, placeholder) is True
    # 后到者还揣着旧值来认领：0 行受影响
    assert await store.claim_cron_task(t["id"], old, placeholder + 1) is False
    row = await store.get_cron_task(t["id"])
    assert row["next_run_at"] == placeholder, "落败方不得覆盖胜方占位值"

    # 值对上了才认领得动：拿当前真实值再来一次应当成功（下一轮槽位语义）
    again = store.compute_next_run({**row, "last_run_at": time.time()})
    assert await store.claim_cron_task(t["id"], placeholder, again) is True


# ---- run_cron_task_core：两个触发方并发，恰好一个执行 ----


async def test_two_trigger_sources_exactly_one_executes(store):
    """应用内扫描（running 字典）与子进程入口（running=None）撞同一到点槽位：
    先到者认领并执行，后到者读到占位值（未来）按「未到点」放弃——不建会话、
    不跑 prompt、不写账本，任务行只有胜方一份回写。
    """
    t = await _add_due_task(store, "双触发去重")

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = {"winner": 0, "loser": 0}

    async def winner_execute(task):
        calls["winner"] += 1
        entered.set()
        await release.wait()  # 认领后在飞，卡住给后来者插手窗口
        return _runtime("胜方产出")

    async def loser_execute(task):
        calls["loser"] += 1
        return _runtime("不该出现的重复执行")

    running: dict = {}
    winner = asyncio.get_running_loop().create_task(run_cron_task_core(
        store, t["id"], execute=winner_execute, running=running))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # 后到方：模拟 cron-run 子进程入口（独立进程没有宿主 running 字典）
    loser = await run_cron_task_core(
        store, t["id"], execute=loser_execute, running=None)
    assert loser is None, "读到认领占位值（未来）的后到方必须放弃"
    assert calls == {"winner": 1, "loser": 0}, "只有先到者真的执行了"

    release.set()
    final = await winner
    assert final is not None and final["last_status"] == "ok"
    assert final["last_result"] == "胜方产出"
    assert final["next_run_at"] > time.time(), "胜方按完成时刻照常重排"

    rows = await store.list_automation_runs(kind="cron", task_id=t["id"])
    assert len(rows) == 1, f"账本必须恰好一行，实际 {len(rows)}"
    assert rows[0]["status"] == "done"


async def test_claim_loser_with_stale_snapshot_skips_execution(store):
    """CAS 落败路径（真实首序竞争）：后到方读到的还是旧到点值，但认领时
    旧值已被先到方改写——core 必须在认领点放弃，不执行、不写账本。
    """
    t = await _add_due_task(store, "旧值落败")
    stale = await store.get_cron_task(t["id"])  # 双方都读过的到点快照
    old = float(stale["next_run_at"])

    # 先到方（另一进程）在后到方认领前把槽位认领走
    placeholder = store.compute_next_run({**stale, "last_run_at": time.time()})
    assert await store.claim_cron_task(t["id"], old, placeholder) is True

    # 后到方进程内还揣着旧快照：core 内部重读拿到的仍是旧值（模拟跨进程
    # 读序——读在先、认领在后），认领 WHERE 旧值落空 → 返回 None
    original_get = store.get_cron_task

    async def stale_read(task_id):
        store.get_cron_task = original_get  # 一次性：只污染第一个内部读
        return dict(stale)

    store.get_cron_task = stale_read
    try:
        async def execute(task):
            raise AssertionError("CAS 落败方不得进入执行")

        loser = await run_cron_task_core(store, t["id"], execute=execute,
                                         running=None)
    finally:
        store.get_cron_task = original_get

    assert loser is None, "认领落败方必须放弃（返回 None）"
    assert await store.list_automation_runs(kind="cron", task_id=t["id"]) == []
    row = await store.get_cron_task(t["id"])
    assert row["next_run_at"] == placeholder, "落败方不得改写胜方占位值"
    assert row["last_status"] == "", "落败方不得写任务行状态"


# ---- 单触发源 / 特殊形态：既有行为零变化 ----


async def test_single_trigger_due_task_unchanged(store):
    """单触发源：到点任务认领必然成功，执行与收尾语义与改造前一致。"""
    t = await _add_due_task(store, "单触发")
    before = float(t["next_run_at"])

    async def execute(task):
        return _runtime("产出")

    final = await run_cron_task_core(store, t["id"], execute=execute)
    assert final is not None, "单触发源不得被认领挡下"
    assert final["last_status"] == "ok" and final["last_result"] == "产出"
    assert final["next_run_at"] > time.time() > before, "照常按完成时刻重排"
    rows = await store.list_automation_runs(kind="cron", task_id=t["id"])
    assert len(rows) == 1 and rows[0]["status"] == "done"


async def test_unscheduled_task_still_runs_like_cli_entry(store):
    """next_run_at=0 的未排期任务照旧可执行：CLI cron run 的既有形态
    （add_cron_task 后不排期直接跑）不因到点判定被误拦。"""
    project = await store.get_or_create_project("/tmp/cron-claim-unsched")
    t = await store.add_cron_task(project.id, "未排期", "干活", "interval",
                                  interval_minutes=30)
    assert t["next_run_at"] == 0

    async def execute(task):
        return _runtime("完成")

    final = await run_cron_task_core(store, t["id"], execute=execute)
    assert final is not None and final["last_status"] == "ok"


async def test_failure_after_claim_keeps_backoff_not_old_value(store):
    """认领后执行失败：错误状态照常回写，next_run_at 按失败时刻重排退避——
    既不恢复旧到点值（无限重试），也不停留在占位值之外的状态。
    """
    t = await _add_due_task(store, "失败退避", interval=30)
    old = float(t["next_run_at"])

    async def boom(task):
        raise RuntimeError("模型不可用")

    final = await run_cron_task_core(store, t["id"], execute=boom)
    assert final["last_status"] == "error"
    assert "模型不可用" in final["last_result"]
    assert final["next_run_at"] > time.time(), "按失败时刻重排，不会立刻重试"
    assert final["next_run_at"] != old, "不得把旧到点值还原成无限重试"
    rows = await store.list_automation_runs(kind="cron", task_id=t["id"])
    assert len(rows) == 1 and rows[0]["status"] == "error"


async def test_claim_gate_tolerance_boundary(store):
    """到点判定的容差边界：提前 10 秒（计划任务抖动范围）照常认领执行；
    提前 10 分钟（或读到他人占位值）按未到点放弃。
    """
    soon = await _add_due_task(store, "容差内", due_offset=10.0)

    async def execute(task):
        return _runtime("提前到点也执行")

    final = await run_cron_task_core(store, soon["id"], execute=execute)
    assert final is not None and final["last_status"] == "ok", \
        "容差内的早到触发按同一槽位认领"

    later = await _add_due_task(store, "未到点", due_offset=600.0)
    calls = {"n": 0}

    async def counting_execute(task):
        calls["n"] += 1
        return _runtime()

    loser = await run_cron_task_core(store, later["id"],
                                     execute=counting_execute)
    assert loser is None and calls["n"] == 0, "未到点不得执行"


async def test_force_bypasses_claim_for_not_due_task(store):
    """force（用户手动立即运行）不认领、不受到点判定拦：语义与改造前一致。"""
    later = await _add_due_task(store, "手动力跑", due_offset=600.0)
    calls = {"n": 0}

    async def execute(task):
        calls["n"] += 1
        return _runtime("手动产出")

    final = await run_cron_task_core(store, later["id"], force=True,
                                     execute=execute)
    assert final is not None and final["last_status"] == "ok"
    assert calls["n"] == 1, "立即运行照常触发"
