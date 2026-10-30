"""电脑控制操作回放（P13）：把一段键鼠动作录成宏，之后一键重放。

安全口径（与「电脑控制默认关」同一条线）：

- **只录「可重放的动作」**：鼠标、键盘、窗口操作、剪贴板写入四类。截屏、
  窗口枚举、剪贴板读取不录——它们是「看」，重放没有意义，反而会把当时的
  屏幕内容/剪贴板内容当动作再执行一遍。
- **录制来源是会话历史**，不是全局键鼠钩子：只录 Agent 自己做过、用户已经
  在确认卡上逐次放行过的动作。不监听用户操作，也就不存在「偷偷记录你的
  键击」这条隐私面。
- **重放必须二次确认**，且先给一份人读预览（第几步、什么动作、参数摘要），
  确认后才真正注入输入。重放过程可随时停（`stop` 事件），每步之间留间隔，
  给用户夺回控制权的窗口。
- 电脑控制工具没注册（开关关着）时一律拒绝：宏不是绕过开关的后门。

存储：`~/.skysheep/macros.json`（引擎自有状态文件，`textio.write_text_atomic`
原子写），同名覆盖、按创建时间保留最近 `MAX_MACROS` 个。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ..messages import ToolUseBlock
from ..textio import write_text_atomic

# 可录制/可重放的工具名（与 tools/computer.py 的注册名一致）
MACRO_ACTIONS = ("mouse", "keyboard", "window", "clipboard_write")
MAX_STEPS = 60          # 单个宏的步数上限（再多也不像「宏」了，更像脚本）
MAX_MACROS = 50         # 保留的宏数量上限
DEFAULT_STEP_DELAY_MS = 400  # 步间隔默认值：太快会让人来不及停
MIN_STEP_DELAY_MS = 50
MAX_STEP_DELAY_MS = 5000
NAME_MAX_CHARS = 40


def macros_path() -> Path:
    from ..config import skysheep_home

    return skysheep_home() / "macros.json"


def _clean_name(raw: object) -> str:
    name = " ".join(str(raw or "").split())[:NAME_MAX_CHARS]
    if not name:
        raise ValueError("宏名称不能为空")
    return name


def load_macros() -> dict[str, dict]:
    """读宏库：`{名字: 宏}`；坏文件/结构不对都按空处理（不让它拖垮设置页）。"""
    try:
        raw = json.loads(macros_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("macros") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {}
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        steps = item.get("steps")
        if not isinstance(steps, list) or not steps:
            continue
        out[str(item["name"])] = item
    return out


def save_macros(items: dict[str, dict]) -> None:
    """原子写宏库；超过上限按创建时间淘汰最旧的（与任务簿同一套裁剪口径）。"""
    rows = sorted(items.values(), key=lambda m: float(m.get("created_at") or 0))
    rows = rows[-MAX_MACROS:]
    write_text_atomic(
        macros_path(),
        json.dumps({"macros": rows}, ensure_ascii=False, indent=2) + "\n",
    )


def put_macro(name: str, steps: list[dict], *, session_id: str = "") -> dict:
    """新建/覆盖一个宏。同名覆盖（用户重录同一段操作很常见）。"""
    clean_name = _clean_name(name)
    clean_steps = normalize_steps(steps)
    if not clean_steps:
        raise ValueError("没有可录制的动作（宏至少要一步）")
    items = load_macros()
    item = {
        "name": clean_name,
        "session_id": str(session_id or ""),
        "created_at": time.time(),
        "steps": clean_steps,
    }
    items[clean_name] = item
    save_macros(items)
    return item


def delete_macro(name: str) -> bool:
    items = load_macros()
    if name not in items:
        return False
    del items[name]
    save_macros(items)
    return True


def normalize_steps(raw: list) -> list[dict]:
    """校验并裁剪步列表：只收可重放动作，参数必须是对象，超长截断。"""
    out: list[dict] = []
    for step in raw or []:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool") or "")
        if tool not in MACRO_ACTIONS:
            continue
        payload = step.get("input")
        if not isinstance(payload, dict):
            continue
        out.append({"tool": tool, "input": dict(payload)})
        if len(out) >= MAX_STEPS:
            break
    return out


def extract_steps(messages) -> list[dict]:
    """从会话历史里提取可重放的动作序列（按时间顺序）。

    只认 assistant 消息里的 ToolUseBlock 且工具名在 MACRO_ACTIONS 里——这正好
    是「Agent 做过的、用户逐次放行过的」那部分动作，不涉及任何用户自己的键击。
    """
    steps: list[dict] = []
    for msg in messages or []:
        for block in getattr(msg, "content", []) or []:
            if not isinstance(block, ToolUseBlock):
                continue
            if block.name not in MACRO_ACTIONS:
                continue
            if not isinstance(block.input, dict):
                continue
            steps.append({"tool": block.name, "input": dict(block.input)})
            if len(steps) >= MAX_STEPS:
                return steps
    return steps


def _clip(text: object, limit: int = 60) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def describe_step(step: dict) -> str:
    """一步的人读摘要（二次确认的预览行，只写关键参数，不堆原始 JSON）。"""
    tool = str(step.get("tool") or "")
    data = step.get("input") or {}
    if tool == "mouse":
        bits = [str(data.get("action") or "")]
        if data.get("x") is not None and data.get("y") is not None:
            bits.append(f"({data.get('x')},{data.get('y')})")
        if data.get("button"):
            bits.append(f"{data['button']} 键")
        return "鼠标 " + " ".join(b for b in bits if b)
    if tool == "keyboard":
        if data.get("text"):
            return "键盘 输入「" + _clip(data["text"]) + "」"
        keys = data.get("keys") or data.get("key") or ""
        if isinstance(keys, (list, tuple)):
            keys = "+".join(str(k) for k in keys)
        return "键盘 按键 " + _clip(keys)
    if tool == "window":
        action = str(data.get("action") or "")
        title = _clip(data.get("title") or data.get("handle") or "")
        return f"窗口 {action}" + (f"「{title}」" if title else "")
    if tool == "clipboard_write":
        return "剪贴板 写入「" + _clip(data.get("text")) + "」"
    return tool


def summarize_steps(steps: list[dict]) -> list[str]:
    return [f"{i}. {describe_step(s)}" for i, s in enumerate(steps, 1)]


async def replay_steps(
    registry, ctx, steps: list[dict], *,
    delay_ms: int = DEFAULT_STEP_DELAY_MS,
    stop: asyncio.Event | None = None,
    on_step=None,
) -> dict:
    """按序执行动作。返回 ``{"executed", "total", "stopped", "results", "error"}``。

    - 工具没注册（电脑控制开关关着 / 平台不支持）：立刻停，报可读原因——
      不静默跳过，否则用户会以为「放完了」。
    - 单步抛错：停下并如实带上那一步的序号与错误，不继续往下注入（半途的
      动作序列比完全不做更危险）。
    - ``stop`` 在步与步之间检查；``on_step`` 用于外推进度（前端逐条显示）。
    """
    delay = max(MIN_STEP_DELAY_MS, min(MAX_STEP_DELAY_MS, int(delay_ms))) / 1000
    results: list[dict] = []
    stopped = False
    error = ""
    for index, step in enumerate(steps, 1):
        if stop is not None and stop.is_set():
            stopped = True
            break
        tool_name = str(step.get("tool") or "")
        tool = registry.get(tool_name) if registry is not None else None
        if tool is None:
            error = (
                f"第 {index} 步「{tool_name}」不可用：电脑控制在设置里没打开"
                "（或在当前平台不可用）。打开后再重放。"
            )
            break
        try:
            args = tool.args_model.model_validate(step.get("input") or {})
            text = await tool.run(args, ctx)
            results.append({"index": index, "tool": tool_name, "ok": True,
                            "preview": _clip(text, 120)})
        except Exception as e:  # noqa: BLE001 - 单步失败要如实报，不吞
            results.append({"index": index, "tool": tool_name, "ok": False,
                            "preview": _clip(e, 120)})
            error = f"第 {index} 步执行失败：{e}"
            break
        if on_step is not None:
            try:
                on_step(results[-1])
            except Exception:  # noqa: BLE001 - 进度回调失败不影响重放
                pass
        if index < len(steps):
            await asyncio.sleep(delay)
    return {
        "executed": len(results),
        "total": len(steps),
        "stopped": stopped,
        "results": results,
        "error": error,
    }
