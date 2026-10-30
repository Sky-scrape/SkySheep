"""电脑控制操作回放（P13）：宏的录制（从会话历史）、删除与重放。

为什么重放走 WS 而不是做成一个工具：

- 宏是**用户自己的快捷方式**（「把刚才那套操作再来一遍」），不是模型能力；
  做成工具等于给模型一条「一次确认执行 N 个键鼠动作」的通路，确认卡上看不出
  里面有什么。
- 重放前必须给完整预览 + 二次确认（`confirm=true` 才真跑），进度逐条广播给
  前端（`macro_progress` / `macro_finished`），随时可停（`macro.stop`）。
- 这台机器上的键鼠注入只对本机有意义：`macro.*` 全部 local_only。

录制来源是会话历史里 Agent 自己的工具调用（用户已逐次放行过），不监听用户
输入；电脑控制开关关着时既不能录（没有动作可录）也不能放（工具没注册）。
"""

from __future__ import annotations

import asyncio

from ...bgtasks import spawn_bg
from ...core import macros as macro_lib
from ...tools import ToolContext

# 单步进度里保留的结果摘要长度（前端只显示一行）
_STEP_PREVIEW_CHARS = 120


class MacrosMixin:
    """宏库与重放（P13）。字段 `_macro_task` / `_macro_stop` 懒建。"""

    def _macro_broadcast(self, ev: dict) -> None:
        try:
            self._ws_broadcast(ev)
        except Exception:  # noqa: BLE001 - 广播失败不影响重放本身
            pass

    async def macro_list(self) -> dict:
        """宏清单（不含步详情，避免设置页一次拉太多）。"""
        items = macro_lib.load_macros()
        rows = [
            {
                "name": name,
                "steps": len(item.get("steps") or []),
                "created_at": float(item.get("created_at") or 0),
                "session_id": str(item.get("session_id") or ""),
            }
            for name, item in items.items()
        ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {
            "macros": rows,
            "max_steps": macro_lib.MAX_STEPS,
            "max_macros": macro_lib.MAX_MACROS,
            "computer_control": bool(self.cfg and self.cfg.computer_control),
            "replaying": self._macro_running(),
        }

    async def macro_get(self, params: dict) -> dict:
        """单个宏的完整步与预览（重放确认弹窗用）。"""
        name = str(params.get("name", "") or "").strip()
        item = macro_lib.load_macros().get(name)
        if item is None:
            raise RuntimeError("找不到宏：" + (name or "（空名）"))
        steps = item.get("steps") or []
        return {
            "name": name,
            "steps": steps,
            "preview": macro_lib.summarize_steps(steps),
        }

    async def macro_save(self, params: dict) -> dict:
        """把某个会话里 Agent 做过的键鼠动作录成宏（同名覆盖）。

        只认 mouse / keyboard / window / clipboard_write 四类（见 core/macros.py
        的 MACRO_ACTIONS）；一个都没录到时报可读错误，不写空宏。
        """
        name = str(params.get("name", "") or "").strip()
        sid = str(params.get("session_id", "") or "").strip()
        if not name:
            raise RuntimeError("给这个宏起个名字")
        if not sid:
            raise RuntimeError("缺少会话 id：宏是从某个会话的历史里录的")
        sess = await self.store.get_session(sid)
        if sess is None:
            raise RuntimeError("会话不存在：" + sid)
        messages = await self.store.load_messages(sid)
        steps = macro_lib.extract_steps(messages)
        if not steps:
            raise RuntimeError(
                "这个会话里没有可录制的电脑控制动作（鼠标 / 键盘 / 窗口 / 写剪贴板）。"
            )
        macro_lib.put_macro(name, steps, session_id=sid)
        return await self.macro_list()

    async def macro_delete(self, params: dict) -> dict:
        name = str(params.get("name", "") or "").strip()
        if not macro_lib.delete_macro(name):
            raise RuntimeError("找不到宏：" + (name or "（空名）"))
        return await self.macro_list()

    def _macro_running(self) -> bool:
        task = getattr(self, "_macro_task", None)
        return bool(task is not None and not task.done())

    async def macro_replay(self, params: dict) -> dict:
        """重放宏。第一次调用（没带 confirm）只回预览，确认后才真正注入输入。"""
        name = str(params.get("name", "") or "").strip()
        item = macro_lib.load_macros().get(name)
        if item is None:
            raise RuntimeError("找不到宏：" + (name or "（空名）"))
        steps = item.get("steps") or []
        if not steps:
            raise RuntimeError("这个宏是空的，没法重放")
        if not bool(params.get("confirm")):
            return {
                "needs_confirm": True,
                "name": name,
                "steps": len(steps),
                "preview": macro_lib.summarize_steps(steps),
                "hint": (
                    "重放会真的注入鼠标键盘动作，先离开键盘、确认屏幕上没有不该动的窗口。"
                    "开始后可以随时点停止。"
                ),
            }
        if not (self.cfg and self.cfg.computer_control):
            raise RuntimeError(
                "电脑控制没打开：先在 设置 · 内置工具 打开「电脑控制」，再重放宏。"
            )
        if self._macro_running():
            raise RuntimeError("已经有一个宏在重放，先停止它再开始新的")
        delay_ms = params.get("delay_ms")
        try:
            delay_ms = int(delay_ms) if delay_ms is not None else macro_lib.DEFAULT_STEP_DELAY_MS
        except (TypeError, ValueError):
            delay_ms = macro_lib.DEFAULT_STEP_DELAY_MS
        sid = str(params.get("session_id", "") or "").strip()
        ctx = ToolContext(working_dir=self.working_dir, session_id=sid)
        registry = self._build_full_registry()
        stop = asyncio.Event()
        self._macro_stop = stop

        def on_step(row: dict) -> None:
            self._macro_broadcast({
                "kind": "macro_progress", "name": name, "session_id": sid,
                "index": row.get("index"), "step_tool": row.get("tool"),
                "ok": bool(row.get("ok")), "preview": row.get("preview", ""),
                "total": len(steps),
            })

        async def _run() -> dict:
            try:
                result = await macro_lib.replay_steps(
                    registry, ctx, steps, delay_ms=delay_ms, stop=stop, on_step=on_step,
                )
            except Exception as e:  # noqa: BLE001 - 兜底：重放异常也要广播终态
                result = {"executed": 0, "total": len(steps), "stopped": False,
                          "results": [], "error": str(e)[:300]}
            self._macro_broadcast({
                "kind": "macro_finished", "name": name, "session_id": sid,
                "executed": result["executed"], "total": result["total"],
                "stopped": bool(result["stopped"]), "error": result.get("error", ""),
            })
            return result

        self._macro_task = spawn_bg(_run())
        return {"started": True, "name": name, "steps": len(steps), "delay_ms": delay_ms}

    async def macro_stop(self) -> dict:
        """请求停止正在重放的宏（步与步之间生效，最多等一个步间隔）。"""
        stop = getattr(self, "_macro_stop", None)
        if not self._macro_running() or stop is None:
            return {"stopping": False}
        stop.set()
        return {"stopping": True}
