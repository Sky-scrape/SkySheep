"""评测基线 · 轮次生命周期场景（fake provider 驱动真实后端管线 / 任务簿）。

两个种子场景：

5. 未决权限被停止 → 事件流出现 decision=cancelled 的 permission_resolved；
6. 任务簿终态记录超限时，list_tasks 保留全部 running/queued。

场景 5 走真实的 ServerBackend.send 管线：decision=cancelled 是后端收尾层
补发的事件（CancelledError 从 pending.wait() 穿透后 agent 侧产不出 resolved），
所以这条必须在后端层评，agent 层复刻不出该行为。
"""

from __future__ import annotations

import asyncio

from skysheep.core.subagent import TaskManager, TaskRecord
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend


async def test_eval_stop_with_pending_permission_resolves_cancelled(home):
    """场景 5（2026-10 审查项 5 场景 B 回归）：停止挂在权限确认上的轮。

    用户点停止时，未决权限的决策永远不会到来。后端收尾必须补发
    decision=cancelled 的 permission_resolved（前端据此收掉残留确认卡），
    轮不产出 turn_finished，残留 request_id 再投递必须失败，文件不落盘。
    """
    be = ServerBackend(
        working_dir=home / "proj",
        provider_factory=lambda: FakeProvider([
            [ToolUseBlock(id="t1", name="write_file",
                          input={"path": "a.txt", "content": "hi"})],
            [TextBlock(text="done")],
        ]),
    )
    await be.setup()
    evs: list[dict] = []

    async def emit(ev: dict) -> None:
        evs.append(ev)

    task = asyncio.create_task(be.send("建个文件", emit))
    try:
        # 等权限请求出来：轮此刻正挂在 pending.wait() 上等决策
        for _ in range(300):
            if any(e.get("kind") == "permission_request" for e in evs):
                break
            await asyncio.sleep(0.01)
        req = next(e for e in evs if e.get("kind") == "permission_request")
        rid = req["request_id"]

        assert be.cancel_run(be.session.id) is True
        r = await task
        assert r["stopped"] is True
        kinds = [e.get("kind") for e in evs]
        assert "turn_finished" not in kinds, "被取消的轮不应产出 turn_finished"
        done = [e for e in evs if e.get("kind") == "permission_resolved"]
        assert len(done) == 1
        assert done[0]["request_id"] == rid
        assert done[0]["decision"] == "cancelled"
        # 与 app.py 的 delivered 契约对齐：残留 request_id 再投递必须失败
        assert be.respond_permission(rid, "allow_once") is False
        # 决策从未执行：文件不存在
        assert not (home / "proj" / "a.txt").exists()
    finally:
        await asyncio.gather(task, return_exceptions=True)
        await be.shutdown()


def _book_task(task_id: str, status: str, created_at: float) -> TaskRecord:
    rec = TaskRecord(task_id=task_id, agent_type="explore", prompt="任务 " + task_id)
    rec.status = status
    rec.created_at = created_at
    return rec


async def test_eval_task_book_keeps_active_when_terminal_exceeds_limit(home):
    """场景 6（任务簿快照截断回归）：终态超限时 running/queued 全量保留。

    任务面板按 tasks.list 轮询 list_tasks 的快照渲染；旧实现先排序再
    recs[-limit:] 尾部截断，终态一多丢掉的恰是排在前头的活动任务——运行中/
    排队的任务从面板凭空消失，也拿不到 task_id（取消/详情/纳入流水线的
    唯一入口）。钉住的行为：活动任务整体保留，终态只挤掉最旧的。
    """
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="x")]]),
        working_dir=home / "proj",
    )
    # 35 条终态 + 5 条运行中 + 1 条排队（跨会话累积，终态 > 默认 limit=30）
    for i in range(35):
        tasks._tasks[f"done{i:02d}"] = _book_task(f"done{i:02d}", "done", created_at=i)
    for i in range(5):
        tasks._tasks[f"run{i}"] = _book_task(f"run{i}", "running", created_at=100 + i)
    tasks._tasks["q0"] = _book_task("q0", "queued", created_at=200)

    out = tasks.list_tasks(limit=30)
    assert len(out) == 30
    active = {r["id"] for r in out if r["status"] in ("running", "queued")}
    assert active == {f"run{i}" for i in range(5)} | {"q0"}, "活动任务一个不能少"
    # 终态保最新 24 条，且整体按新→旧展示
    done_ids = [r["id"] for r in out if r["status"] == "done"]
    assert done_ids == [f"done{i:02d}" for i in range(34, 10, -1)]
