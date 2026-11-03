"""运行巡检（Watchdog 的本地便宜版）：批次过后回头看「跑完了，但跑歪了吗」。

为什么不做常驻 Watchdog：那种设计是给「7×24 云端常驻」的系统用的。本地应用
常年不常驻，为一个常驻巡检会话付出一整条对话的 token 并不划算。

但盲区是真的：``run_timeout_minutes`` 只管跑完没跑完，
``alert_after_failures`` 只管错没错，两者都不回答「跑完了但跑歪了」——
模型在一个不存在的问题上转了四十轮、最后写一份自我感动的总结，现在会被
记成 ``outcome=ok``。

做法：每 ``interval_hours``（默认 12 小时）最多跑一次**事后**巡检——把最近
``hours``（默认 24 小时）的终态运行压成一张紧凑表，问模型一个问题：
「这些运行里有没有明显的无效循环、或者偏离任务目标的？」结论落盘成报告。

边界（照抄会出事的地方，明确不做）：
- **只建议、不自动改配置**：自动改 ``[factory]`` 开关风险太大；
- 只吃账本里已有的数字，不读会话正文——巡检不该变成第二个能看你全部
  聊天记录的通道；
- 默认关：巡检要真金白银调模型，用户没开就等于没有。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ...config import skysheep_home
from ...textio import write_text_atomic

logger = logging.getLogger("skysheep.security")

STATE_FILE = "run_inspect.json"
DEFAULT_INTERVAL_HOURS = 12
DEFAULT_WINDOW_HOURS = 24
# 少于这么多条终态运行就不巡：样本太少，模型只能编。
MIN_RUNS = 3
# 喂给模型的条数上限：40 行已经够看出重复模式，再多只会稀释注意力。
MAX_RUNS_FED = 40
MAX_REPORT_CHARS = 12_000

INSPECT_PROMPT = (
    "你是自动化运行的巡检员。下面是最近一批自动任务运行记录，一行一条，"
    "字段：产线 | 状态 | 做成判定 | 错误类型 | 被权限拦下次数 | 时长 | 输入/输出 token | 产出摘要。\n\n"
    "只回答一个问题：这些运行里有没有「无效循环」或「偏离任务目标」的迹象？\n"
    "判定线索：同一产线反复失败且错误类型相同；被权限拦下很多次还在重试；"
    "跑了很久但产出摘要为空或与任务无关；耗时/输出 token 远超同批中位数却无产出。\n\n"
    "要求：\n"
    "- 没有迹象就只输出一行「无需留意」；\n"
    "- 有就每条一行，格式 `- <产线> | <现象> | <建议动作>`，最多 5 条；\n"
    "- 不要评价代码质量，不要展开分析，不要给无关建议。\n"
)


def state_path() -> Path:
    return skysheep_home() / STATE_FILE


def reports_dir() -> Path:
    return skysheep_home() / "reports"


def default_state() -> dict:
    return {
        "enabled": False,
        "interval_hours": DEFAULT_INTERVAL_HOURS,
        "hours": DEFAULT_WINDOW_HOURS,
        "last_at": 0.0,
        "last_summary": "",
        "last_report": "",
    }


def load_state() -> dict:
    state = default_state()
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    state["enabled"] = raw.get("enabled", False) is True
    for key, lo, hi in (
        ("interval_hours", 1, 168),
        ("hours", 1, 744),
    ):
        try:
            val = int(raw.get(key, state[key]))
        except (TypeError, ValueError):
            continue
        state[key] = val if lo <= val <= hi else state[key]
    try:
        state["last_at"] = float(raw.get("last_at") or 0.0)
    except (TypeError, ValueError):
        state["last_at"] = 0.0
    state["last_summary"] = str(raw.get("last_summary") or "")[:2000]
    state["last_report"] = str(raw.get("last_report") or "")
    return state


def save_state(state: dict) -> None:
    clean = default_state()
    clean["enabled"] = bool(state.get("enabled", False))
    for key in ("interval_hours", "hours"):
        try:
            clean[key] = int(state.get(key, clean[key]))
        except (TypeError, ValueError):
            pass
    try:
        clean["last_at"] = float(state.get("last_at") or 0.0)
    except (TypeError, ValueError):
        clean["last_at"] = 0.0
    clean["last_summary"] = str(state.get("last_summary") or "")[:2000]
    clean["last_report"] = str(state.get("last_report") or "")
    try:
        write_text_atomic(state_path(), json.dumps(clean, ensure_ascii=False, indent=2))
    except OSError as e:  # noqa: BLE001 - 状态写不进去不该炸巡检调用方
        logger.warning("巡检状态写入失败：%s", e)


def _clip(text, limit: int = 80) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def build_digest(runs: list[dict]) -> str:
    """把运行压成一行一条的紧凑表（模型看它，不看原始 JSON）。"""
    lines = []
    for r in runs[:MAX_RUNS_FED]:
        kind = str(r.get("kind") or "")
        line_id = r.get("task_id") if kind == "cron" else r.get("pipeline_id")
        dur = float(r.get("duration_s") or 0)
        lines.append(
            " | ".join([
                f"{kind}#{line_id}",
                str(r.get("status") or ""),
                str(r.get("outcome") or "-"),
                str(r.get("error_kind") or "-"),
                str(r.get("denied_count") or 0),
                f"{dur / 60:.1f}min",
                f"{int(r.get('in_tokens') or 0)}/{int(r.get('out_tokens') or 0)}",
                _clip(r.get("result_excerpt") or r.get("error") or "-", 60),
            ])
        )
    return "\n".join(lines)


def _report_path(now: float) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return reports_dir() / f"inspect-{stamp}.md"


def _prune_reports(keep: int = 30) -> None:
    """只留最近若干份巡检报告（本机文件，别无限长）。"""
    try:
        files = sorted(reports_dir().glob("inspect-*.md"))
    except OSError:
        return
    for old in files[:-max(1, int(keep))]:
        try:
            old.unlink()
        except OSError:
            pass


async def inspect_runs(store, provider, *, hours: int | None = None,
                       now: float | None = None) -> dict:
    """跑一次巡检并返回 ``{ok, text, report, runs, reason}``。

    纯函数式：不读写开关状态（那是调用方的事），便于手动触发与测试。
    """
    n = time.time() if now is None else float(now)
    state = load_state()
    window = int(hours or state["hours"])
    runs = await store.automation_runs_since(n - window * 3600)
    done = [r for r in runs if str(r.get("status") or "") not in ("running", "queued")]
    if len(done) < MIN_RUNS:
        return {"ok": False, "reason": "too_few_runs", "runs": len(done), "text": ""}
    if provider is None:
        return {"ok": False, "reason": "no_provider", "runs": len(done), "text": ""}

    digest = build_digest(done)
    prompt = f"{INSPECT_PROMPT}\n窗口：最近 {window} 小时，共 {len(done)} 条终态运行。\n\n{digest}"
    from ._shared import collect_stream_text  # 局部导入：避免模块级环

    text = ""
    try:
        async with asyncio.timeout(90):
            text = await collect_stream_text(provider, prompt)
    except Exception as e:  # noqa: BLE001 - 巡检失败只记日志，不打扰任何人
        logger.warning("运行巡检调用失败：%s", e)
        return {"ok": False, "reason": "provider_error", "runs": len(done), "text": ""}
    text = (text or "").strip()[:MAX_REPORT_CHARS]
    if not text:
        return {"ok": False, "reason": "empty", "runs": len(done), "text": ""}

    report = _report_path(n)
    body = (
        f"# 运行巡检 {time.strftime('%Y-%m-%d %H:%M', time.localtime(n))}\n\n"
        f"窗口：最近 {window} 小时，终态运行 {len(done)} 条\n\n"
        f"## 结论\n\n{text}\n\n## 原始记录（{min(len(done), MAX_RUNS_FED)} 条）\n\n"
        f"```\n{digest}\n```\n"
    )
    try:
        write_text_atomic(report, body)
        _prune_reports()
    except OSError as e:  # noqa: BLE001 - 报告写不进去也算巡检成功（结果已在返回里）
        logger.warning("巡检报告落盘失败：%s", e)
        report = None  # type: ignore[assignment]

    fresh = load_state()
    fresh["last_at"] = n
    fresh["last_summary"] = text
    fresh["last_report"] = str(report or "")
    save_state(fresh)
    return {
        "ok": True, "reason": "ok", "runs": len(done), "text": text,
        "report": str(report or ""),
    }


async def run_inspect_pass(backend, *, now: float | None = None, push=None) -> dict:
    """定时巡检的一轮检查：开关开、距上次够久、样本够 → 跑一次并推渠道。

    返回 ``{ran: bool, reason: str}``。任何异常都不外抛——它跑在周期循环里，
    抛出去只会把循环带下来（与日报同一姿态）。
    """
    n = time.time() if now is None else float(now)
    state = load_state()
    if not state.get("enabled"):
        return {"ran": False, "reason": "disabled"}
    if n - float(state.get("last_at") or 0) < state["interval_hours"] * 3600:
        return {"ran": False, "reason": "not_due"}
    try:
        res = await inspect_runs(
            backend.store, getattr(backend, "provider", None),
            hours=state["hours"], now=n,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("运行巡检异常：%s", e)
        return {"ran": False, "reason": "error"}
    if not res.get("ok"):
        # 样本不够不算「跑过」：别把 last_at 推后，攒够了下次再来
        return {"ran": False, "reason": str(res.get("reason") or "skip")}
    if push is not None:
        try:
            await push(f"🧭 运行巡检\n\n{res['text']}", what="运行巡检")
        except Exception as e:  # noqa: BLE001 - 推送失败不影响已落盘的结论
            logger.warning("运行巡检推送失败：%s", e)
    return {"ran": True, "reason": "ok", "text": res["text"]}
