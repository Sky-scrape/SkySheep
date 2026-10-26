"""审计 SEC-M15 回归：后台子代理被停止时，已流出的部分报告不再整体丢弃。

修复前：run_subagent 被取消时 CancelledError 直接上抛，final_text 丢弃；
_run_one 的取消分支只记账 token 不回收正文——与圆桌/团队「用户已看到的
不凭空消失」语义不一致。修复后：run_subagent 从 agent.history 收割兜底正文
（Agent 层 run_turn 的 finally 已把流式中断正文并进历史，_persist_partial_
stream_text 产物形态），挂在取消异常上带出；_run_one 的取消分支写进任务
记录并照常走 _maybe_write_report 落盘，然后原样上抛取消（取消语义不吞）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from skysheep.core.subagent import TaskManager, run_subagent
from skysheep.messages import TextBlock
from skysheep.models.base import ProviderDone, ProviderTextDelta, ProviderToolUse
from skysheep.models.fake import FakeProvider

# 单帧约 570 字符 × 6 帧 ≈ 3.4k 字符 > REPORT_INLINE_LIMIT(2400)：取消后长报告要落盘
_FRAME = "流式正文片段。" * 80


class TwoPhaseStreamProvider(FakeProvider):
    """两段式假模型：先完成一次只读工具调用，再流式吐数帧正文后挂起。

    第二段把数帧 text_delta 全部吐完后停在永不完成的 wait 上——外层取消
    将落在这个挂起点，复现「流式中途被停止」的现场（取消穿透生成器栈，
    run_turn 的 finally 先把已流出正文并进历史，run_subagent 再收割）。
    """

    def __init__(self) -> None:
        super().__init__([])
        self.calls = 0
        self.hang_reached = asyncio.Event()

    async def stream(self, messages, tool_schemas, effort=None) -> AsyncIterator:
        self.calls += 1
        if self.calls == 1:
            # 第一段：一次只读工具调用（list_dir 只读自动放行，不弹确认）
            yield ProviderToolUse(id="t1", name="list_dir", input={"path": "."})
            yield ProviderDone(stop_reason="tool_use", input_tokens=11, output_tokens=7)
            return
        # 第二段：流式正文数帧后挂起（hang_reached 置位时六帧均已并入
        # Agent 的流式缓冲，收割有据可依）
        for i in range(6):
            yield ProviderTextDelta(f"PARTIAL-{i} {_FRAME}")
            await asyncio.sleep(0)
        self.hang_reached.set()
        await asyncio.Event().wait()  # 永远挂起：等外层 cancel 打断


async def _wait_event(ev: asyncio.Event, timeout: float = 5.0) -> None:
    await asyncio.wait_for(ev.wait(), timeout)


async def test_cancel_recovers_streamed_partial(tmp_path):
    """停止后台子代理：已流出正文写进任务记录（带取消标注）并照常落盘。"""
    provider = TwoPhaseStreamProvider()
    tasks = TaskManager(provider_factory=lambda: provider, working_dir=tmp_path)
    task_id = tasks.start_background("explore", "慢慢写报告")
    rec = tasks.status(task_id)
    await _wait_event(provider.hang_reached)  # 六帧已全部流出、模型已挂起

    tasks.cancel_task(task_id)
    with contextlib.suppress(asyncio.CancelledError):
        await rec.asyncio_task

    assert rec.status == "cancelled"
    assert rec.error == "用户取消"
    # 已流出的正文进了任务记录，带「已取消」标注（与圆桌取消标记同风格）
    assert "已取消" in rec.result
    assert "PARTIAL-0" in rec.result and "PARTIAL-5" in rec.result
    # 长报告照常走 _maybe_write_report 落盘，文件含已流出正文
    assert rec.report_path
    body = Path(rec.report_path).read_text(encoding="utf-8")
    assert "PARTIAL-0" in body and "PARTIAL-5" in body
    # 任务簿详情能看到回收的正文（cancelled 不走 check_task 投递，详情是唯一出口）
    assert "PARTIAL-0" in tasks.get_detail(task_id)["result"]


async def test_normal_completion_untouched(tmp_path):
    """正常完成路径零变化：result 是完整正文、无取消标注，短报告不落盘。"""
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="完整报告正文")]]),
        working_dir=tmp_path,
    )
    task_id = tasks.start_background("explore", "正常跑")
    rec = tasks.status(task_id)
    for _ in range(200):
        if rec.status == "done":
            break
        await asyncio.sleep(0.01)
    assert rec.status == "done"
    assert rec.result == "完整报告正文"
    assert "已取消" not in rec.result
    assert rec.report_path == ""


async def test_cancelled_error_still_propagates(tmp_path):
    """取消语义必须保持：CancelledError 穿透 run_subagent 上抛，不被吞掉。"""
    provider = TwoPhaseStreamProvider()
    task = asyncio.create_task(run_subagent(
        provider=provider, working_dir=tmp_path, agent_type="explore", prompt="慢慢写",
    ))
    await _wait_event(provider.hang_reached)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled(), "run_subagent 吞掉了 CancelledError，取消语义被破坏"
