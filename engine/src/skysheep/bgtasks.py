"""后台任务的强引用登记：不让「发出去就不管」的任务被 GC 掉。

背景（安全审查低危项）：``asyncio`` 的事件循环对任务只持**弱引用**，官方文档
明确要求调用方自己保存返回值——``asyncio.create_task(coro)`` 的返回值没人接住时，
任务可能在跑到一半时被垃圾回收，表现为「偶尔没生效」「偶尔没发出去」这类极难
复现的问题。通知、冲刷、后台收尾这类 fire-and-forget 任务最容易踩到。

用法：凡是「不需要等待、也不打算在别处保存引用」的后台任务，一律走
:func:`spawn_bg`；需要在别处保存引用（例如存进 ``buf["timer"]`` 供取消）的任务
仍用 ``create_task`` 自己管。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

# 未结束的后台任务集合：持有强引用，任务结束时自动移除
_BG_TASKS: set[asyncio.Task] = set()

logger = logging.getLogger("skysheep.bg")


def _on_bg_done(task: asyncio.Task) -> None:
    """登记表移除 + 异常留痕。

    fire-and-forget 的任务异常没人检索（Task 对象会吞住它直到被 GC），
    表现为「广播失败、后台收尾失败」在日志里无影无踪。这里在回收引用时
    顺带把异常打进日志（取消不算异常——用户停止是正常路径）。
    """
    _BG_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        coro = getattr(task, "get_coro", lambda: None)()
        origin = getattr(coro, "__qualname__", "") or repr(coro)
        logger.error("后台任务异常（%s）：%s: %s", origin, type(exc).__name__, exc,
                     exc_info=exc)


def spawn_bg(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """创建后台任务并持强引用，直到它结束（结束即从登记表移除）。"""
    task = asyncio.get_running_loop().create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_on_bg_done)
    return task


async def drain_bg_tasks(timeout: float = 30.0) -> int:
    """有界等待全部在册后台任务落地，返回放弃等待的数量（超时未归者取消）。

    一次性进程（`skysheep cron run`，Windows 计划任务拉起的无人值守入口）
    的退出前收尾用：fire-and-forget 的终态推送若不等它落地，asyncio.run
    收尾会统一取消在途任务——CancelledError 是 BaseException，渠道适配器
    的 ``except Exception`` 接不住，_on_bg_done 对取消又静默返回，推送就
    丢了且无任何日志。退出前有界等一轮，正常推送都能落地；超时未归的
    放弃（记日志并取消），不让坏端点把进程退出拖死。
    """
    pending = [t for t in list(_BG_TASKS) if not t.done()]
    if not pending:
        return 0
    _done, late = await asyncio.wait(pending, timeout=timeout)
    for t in late:
        coro = getattr(t.get_coro(), "__qualname__", "") or repr(t)
        logger.warning("退出收尾：后台任务 %s 超过 %s 秒未落地，放弃等待并取消",
                       coro, timeout)
        t.cancel()
    return len(late)


def pending_count() -> int:
    """当前登记在册的后台任务数（测试与诊断用）。"""
    return len(_BG_TASKS)


__all__ = ["drain_bg_tasks", "pending_count", "spawn_bg"]
