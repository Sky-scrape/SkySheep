"""每日运行日报：当天全部定时任务 / 任务编排终态的中文汇总，每天定时推一次。

与「终态推送」（notify_channel 随事件触发）的分工：那是事件驱动的即时通知；
日报是时间驱动的每日汇总——一天跑了什么、成了几件、败在哪儿，一条看完，
给无人值守场景当「今日体检表」。

状态（开关、推送时刻、上次已发日期）存引擎自有状态文件
``~/.skysheep/daily_report.json``（``textio.write_text_atomic`` 原子写），
不走 config.toml：日报只有「一个开关 + 一个时刻 + 一条防重发记录」，
进配置文件反而要为它开设置段。开关**默认关**；「上次已发日期」是防重发键
——当天推过一次（含送达失败的尝试）就不再推。日报是低价值摘要，为它做
全天重试不值得：无目标渠道或端点不可达时明天的日报照常，不做补发风暴。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from ...config import skysheep_home
from ...textio import write_text_atomic

logger = logging.getLogger("skysheep.security")

STATE_FILE = "daily_report.json"
DEFAULT_REPORT_TIME = "09:00"
# 周报（P10 剩余半边）：每 7 天一条，窗口是「今天往前数 7 天（含今天）」。
# 与日报共用同一个状态文件——一张卡片管两种节律，少一个要同步的地方。
WEEK_DAYS = 7
DEFAULT_WEEKLY_WEEKDAY = 0  # 0=周一 … 6=周日（与 datetime.weekday() 同口径）
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 周报汇总的运行条数上限（automation_runs 的分页上限 500）：超过只统计最近 500 条，
# 报告里如实标注「仅统计最近 N 条」，不假装是全量。
WEEKLY_RUN_LIMIT = 500

# 失败明细行里单条原因的截断长度（日报要一眼看完，明细只给指路的第一句）
DETAIL_CHARS = 120
# 自定义模板的长度上限（P10）：日报走的是渠道文本小件，2000 字符够写花样也防手滑灌满
TEMPLATE_MAX_CHARS = 2000


def state_path() -> Path:
    return skysheep_home() / STATE_FILE


def default_state() -> dict:
    return {
        "enabled": False, "time": DEFAULT_REPORT_TIME, "last_sent_date": "",
        # 周报三件套：开关 / 周几 / 时刻；last_sent_date 记的是「本周已发的日期」，
        # 与日报分开记，互不影响各自的防重发判定
        "weekly_enabled": False,
        "weekly_time": DEFAULT_REPORT_TIME,
        "weekly_weekday": DEFAULT_WEEKLY_WEEKDAY,
        "weekly_last_sent_date": "",
    }


def load_state() -> dict:
    """读日报状态；缺文件 / 坏 JSON / 字段缺失都回落默认值（开关默认关）。"""
    state = default_state()
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    # 开关只认真布尔：手改成 "yes"/1 这类值一律按关处理（宁可不推，不可误推）
    state["enabled"] = raw.get("enabled", False) is True
    if parse_hhmm(raw.get("time")) is not None:
        state["time"] = str(raw["time"]).strip()
    state["last_sent_date"] = str(raw.get("last_sent_date") or "")
    # 周报：开关同样只认真布尔；周几只认 0..6 的整数（填错回落周一），
    # 时刻复用日报的 HH:MM 校验
    state["weekly_enabled"] = raw.get("weekly_enabled", False) is True
    if parse_hhmm(raw.get("weekly_time")) is not None:
        state["weekly_time"] = str(raw["weekly_time"]).strip()
    try:
        wd = int(raw.get("weekly_weekday", DEFAULT_WEEKLY_WEEKDAY))
    except (TypeError, ValueError):
        wd = DEFAULT_WEEKLY_WEEKDAY
    state["weekly_weekday"] = wd if 0 <= wd <= 6 else DEFAULT_WEEKLY_WEEKDAY
    state["weekly_last_sent_date"] = str(raw.get("weekly_last_sent_date") or "")
    template = raw.get("template")
    if isinstance(template, str) and template.strip():
        state["template"] = template.strip()[:TEMPLATE_MAX_CHARS]
    weekly_template = raw.get("weekly_template")
    if isinstance(weekly_template, str) and weekly_template.strip():
        state["weekly_template"] = weekly_template.strip()[:TEMPLATE_MAX_CHARS]
    return state


def save_state(state: dict) -> None:
    """原子写日报状态（引擎自有状态文件一律原子写，见 textio.write_text_atomic）。

    未知字段不落盘：写出去的形状永远是自己读得回来的那几个键
    （enabled / time / last_sent_date / weekly_* / 可选 template）。
    """
    clean = default_state()
    clean["enabled"] = bool(state.get("enabled", False))
    hhmm = state.get("time")
    clean["time"] = (
        str(hhmm).strip() if parse_hhmm(hhmm) is not None else DEFAULT_REPORT_TIME
    )
    clean["last_sent_date"] = str(state.get("last_sent_date") or "")
    clean["weekly_enabled"] = bool(state.get("weekly_enabled", False))
    whhmm = state.get("weekly_time")
    clean["weekly_time"] = (
        str(whhmm).strip() if parse_hhmm(whhmm) is not None else DEFAULT_REPORT_TIME
    )
    try:
        wd = int(state.get("weekly_weekday", DEFAULT_WEEKLY_WEEKDAY))
    except (TypeError, ValueError):
        wd = DEFAULT_WEEKLY_WEEKDAY
    clean["weekly_weekday"] = wd if 0 <= wd <= 6 else DEFAULT_WEEKLY_WEEKDAY
    clean["weekly_last_sent_date"] = str(state.get("weekly_last_sent_date") or "")
    template = state.get("template")
    if isinstance(template, str) and template.strip():
        clean["template"] = template.strip()[:TEMPLATE_MAX_CHARS]
    weekly_template = state.get("weekly_template")
    if isinstance(weekly_template, str) and weekly_template.strip():
        clean["weekly_template"] = weekly_template.strip()[:TEMPLATE_MAX_CHARS]
    write_text_atomic(state_path(), json.dumps(clean, ensure_ascii=False, indent=2) + "\n")


def parse_hhmm(raw) -> tuple[int, int] | None:
    """「HH:MM」（24 小时制，容忍「9:05」单数字写法）→ (hour, minute)；不合法返回 None。"""
    try:
        text = str(raw or "").strip()
        h_s, sep, m_s = text.partition(":")
        if not sep:
            return None
        h, m = int(h_s), int(m_s)
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def today_str(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _day_bounds(now: float) -> tuple[float, float]:
    """本地时区「今天」的 [当天零点, 次日零点) epoch 区间。

    终点必须按日历推「次日本地零点」（datetime + timedelta(days=1) 再取
    timestamp），不能用固定 86400 秒外推：DST 切换日本地天长是 23/25 小时，
    固定外推会让窗口提前/错后 1 小时收口——日报与运行总览的当天终态漏计
    或重复计。端点零点落在本地钟面上，交由平台按当地规则取对应该时刻的
    epoch（datetime.timestamp 的语义）。
    """
    dt = datetime.fromtimestamp(now)
    start = datetime(dt.year, dt.month, dt.day).timestamp()
    end = (datetime(dt.year, dt.month, dt.day) + timedelta(days=1)).timestamp()
    return start, end


def report_time_passed(state: dict, now: float) -> bool:
    """现在是否已到（或过了）当天配置的推送时刻。时刻不合法回落默认 09:00。"""
    hhmm = parse_hhmm(state.get("time")) or (9, 0)
    dt = datetime.fromtimestamp(now)
    return (dt.hour, dt.minute) >= hhmm


def _clip(text, limit: int = DETAIL_CHARS) -> str:
    """压空白 + 截断：失败原因只留第一句指路的。"""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…（已截断）"


def _failure_detail(cron_bad: list[dict], pipe_bad: list[dict]) -> list[str]:
    """失败明细行：每个失败的定时任务 / 流水线各一行（原因截断）。

    流水线行点出未成功节点数与第一个失败节点的标题 + 原因；「用户停止」
    型流水线（cancelled）不算失败，不进明细。
    """
    out: list[str] = []
    for t in cron_bad:
        reason = _clip(t.get("last_result")) or "（无原因记录）"
        out.append(f"· 定时任务「{t.get('name') or '未命名任务'}」失败：{reason}")
    for p in pipe_bad:
        bad_nodes = [
            nd for nd in (p.get("nodes") or [])
            if nd.get("status") not in ("done", "skipped")
        ]
        tail = ""
        if bad_nodes:
            first = bad_nodes[0]
            reason = _clip(first.get("last_error") or first.get("result")) or "（无原因记录）"
            tail = f"：{first.get('title') or '未命名节点'}——{reason}"
            if len(bad_nodes) > 1:
                tail += f"（另有 {len(bad_nodes) - 1} 个节点未成功）"
        out.append(
            f"· 流水线「{p.get('name') or '未命名流水线'}」失败，"
            f"{len(bad_nodes)} 个节点未成功{tail}"
        )
    return out


async def build_daily_report(store, *, now: float | None = None, template: str | None = None) -> str:
    """把当天全部 cron / 流水线终态汇总成一条中文日报（成功/失败计数 + 失败明细）。

    数据口径：cron_tasks 表只留每个任务的**最后一次**运行（last_run_at 落在
    今天即计入，一天跑多次只见最后一次）；流水线按 finished_at 落在今天且已
    到终态（done / failed / cancelled）计入，运行中与草稿不算。全项目口径
    ——日报是整机的无人值守体检表，不按项目拆。

    template（P10 模板自定义）：非空时按用户模板渲染——占位符 {date}
    {cron_ok} {cron_fail} {pipe_done} {pipe_fail} {pipe_stop} {detail} 直替
    （不用 str.format：模板里自带的其它花括号原样保留，未知占位不炸）；
    {detail} 展开为失败明细多行（每行缩进两格，无失败 = 「无」）。
    None/空白 = 内置格式。空天也照渲染（占位符落 0 / 无），不用内置的
    「今日没有运行记录」短句——模板用户自己决定空天长什么样。
    """
    n = time.time() if now is None else float(now)
    start, end = _day_bounds(n)

    cron_rows = [
        t for t in await store.list_cron_tasks()
        if start <= float(t.get("last_run_at") or 0) < end
    ]
    cron_ok = [t for t in cron_rows if t.get("last_status") in ("ok", "empty")]
    cron_bad = [t for t in cron_rows if t.get("last_status") == "error"]

    pipes = [
        p for p in await store.list_pipelines()
        if p.get("status") in ("done", "failed", "cancelled")
        and start <= float(p.get("finished_at") or 0) < end
    ]
    pipe_ok = [p for p in pipes if p["status"] == "done"]
    pipe_bad = [p for p in pipes if p["status"] == "failed"]
    pipe_stop = [p for p in pipes if p["status"] == "cancelled"]

    detail = _failure_detail(cron_bad, pipe_bad)
    if template and template.strip():
        filled = template
        repl = {
            "date": today_str(n),
            "cron_ok": len(cron_ok), "cron_fail": len(cron_bad),
            "pipe_done": len(pipe_ok), "pipe_fail": len(pipe_bad),
            "pipe_stop": len(pipe_stop),
            "detail": "\n".join("  " + d for d in detail) if detail else "无",
        }
        for key, val in repl.items():
            filled = filled.replace("{" + key + "}", str(val))
        return filled

    lines = [f"🐑 SkySheep 运行日报 · {today_str(n)}"]
    if not cron_rows and not pipes:
        lines.append("今日没有定时任务或任务编排的运行记录。")
        return "\n".join(lines)
    lines.append(f"定时任务：成功 {len(cron_ok)} / 失败 {len(cron_bad)}")
    lines.append(
        f"任务编排：完成 {len(pipe_ok)} / 失败 {len(pipe_bad)} / 已停止 {len(pipe_stop)}"
    )
    if detail:
        lines.append("失败明细：")
        lines.extend(detail)
    else:
        lines.append("失败明细：无")
    return "\n".join(lines)


async def run_daily_report_pass(
    store, mgr, *, now: float | None = None, push=None,
) -> dict:
    """日报的一轮检查：开关开、已到点、今天没发过 → 汇总并推一次，记下日期。

    推送复用既有目标逻辑（push_text_to_targets，缺省时局部导入避免环）。
    返回 ``{"sent": bool, "reason": str}``（reason: disabled / already_sent /
    not_due / ok），sent=True 时附 body。**无论送达与否都记「上次已发日期」**
    ——日报每天只尝试一次，无目标渠道或端点不可达都不重试（防给不可达的
    地址刷一整天请求；明天的日报照常）。
    """
    n = time.time() if now is None else float(now)
    state = load_state()
    if not state.get("enabled"):
        return {"sent": False, "reason": "disabled"}
    today = today_str(n)
    if state.get("last_sent_date") == today:
        return {"sent": False, "reason": "already_sent"}
    if not report_time_passed(state, n):
        return {"sent": False, "reason": "not_due"}
    body = await build_daily_report(store, now=n, template=state.get("template"))
    if push is None:
        from .automation import push_text_to_targets  # 局部导入避免模块环

        push = push_text_to_targets
    try:
        await push(mgr, body, what="运行日报")
    except Exception as e:  # noqa: BLE001 - 推送出口的异常也不影响记日期
        logger.warning("运行日报推送失败：%s", e)
    fresh = load_state()  # 写前重读合并：避免覆盖并发期间保存的开关改动
    fresh["last_sent_date"] = today
    save_state(fresh)
    return {"sent": True, "reason": "ok", "body": body}


# ---- 周报（P10 剩余半边）：每 7 天一条的运行汇总 ----
# 与日报的分工：日报回答「昨天跑得怎么样」，周报回答「这一周的趋势」——数据源
# 必须换成运行账本 automation_runs（cron_tasks 每个任务只留最后一次，天然数不出
# 一周）。窗口是「今天往前数 7 天（含今天）」，与日报的「本地日历天」同一套
# 边界算法（datetime 推日，不用固定 86400 秒外推，DST 日不会错一小时）。


def _week_bounds(now: float) -> tuple[float, float]:
    """本地时区「最近 7 天（含今天）」的 [起点零点, 次日元旦零点) epoch 区间。"""
    dt = datetime.fromtimestamp(now)
    start = (datetime(dt.year, dt.month, dt.day) - timedelta(days=WEEK_DAYS - 1)).timestamp()
    end = (datetime(dt.year, dt.month, dt.day) + timedelta(days=1)).timestamp()
    return start, end


def weekly_due(state: dict, now: float) -> bool:
    """周报是否该发了：今天是配置的星期几，且已到配置时刻。

    周几/时刻都不合法时回落默认值（周一 09:00）——与日报「时刻不合法回落
    默认」同姿态；界面上填错会当场报错，这里兜的是手改状态文件的情况。
    """
    try:
        wd = int(state.get("weekly_weekday", DEFAULT_WEEKLY_WEEKDAY))
    except (TypeError, ValueError):
        wd = DEFAULT_WEEKLY_WEEKDAY
    if not 0 <= wd <= 6:
        wd = DEFAULT_WEEKLY_WEEKDAY
    dt = datetime.fromtimestamp(now)
    if dt.weekday() != wd:
        return False
    hhmm = parse_hhmm(state.get("weekly_time")) or (9, 0)
    return (dt.hour, dt.minute) >= hhmm


def _run_failed(row: dict) -> bool:
    """运行账本行的失败口径（与工厂/告警同一套，见 automation.alert_repeated_failures）。"""
    status = str(row.get("status") or "")
    if status in ("running", "skipped", "cancelled"):
        return False
    return status in ("error", "interrupted", "empty") or str(row.get("outcome") or "") == "failed"


async def build_weekly_report(store, *, now: float | None = None, template: str | None = None) -> str:
    """最近 7 天的运行汇总（成功/失败/跳过计数 + 失败原因 Top 3 + 最忙的一天）。

    数据口径：``automation_runs`` 里 started_at 落在窗口内的运行行（整机口径，
    不按项目拆）；一次查询最多取 WEEKLY_RUN_LIMIT 条，取满时报告如实标注
    「仅统计最近 N 条」而不是假装全量。
    """
    n = time.time() if now is None else float(now)
    start, end = _week_bounds(n)
    rows = await store.list_automation_runs(
        started_since=start, limit=WEEKLY_RUN_LIMIT,
    )
    rows = [r for r in rows if float(r.get("started_at") or 0) < end]
    truncated = len(rows) >= WEEKLY_RUN_LIMIT

    ok = failed = stopped = 0
    kind_count: dict[str, int] = {}
    day_count: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "")
        if status in ("running", "skipped", "cancelled"):
            stopped += 1
        elif _run_failed(row):
            failed += 1
            why = _clip(row.get("error") or row.get("result_excerpt")) or "（无原因记录）"
            reasons[why] = reasons.get(why, 0) + 1
        else:
            ok += 1
        kind = str(row.get("kind") or "other")
        kind_count[kind] = kind_count.get(kind, 0) + 1
        day = time.strftime("%Y-%m-%d", time.localtime(float(row.get("started_at") or 0)))
        day_count[day] = day_count.get(day, 0) + 1

    range_text = (
        time.strftime("%Y-%m-%d", time.localtime(start))
        + " ~ " + time.strftime("%Y-%m-%d", time.localtime(n))
    )
    top_reasons = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    detail = "\n".join(f"· {cnt} 次：{why}" for why, cnt in top_reasons)
    busiest = ""
    if day_count:
        day, cnt = max(day_count.items(), key=lambda kv: (kv[1], kv[0]))
        busiest = f"{day}（{cnt} 次）"
    kind_text = "、".join(
        f"{KIND_LABELS.get(k, k)} {v}" for k, v in sorted(kind_count.items(), key=lambda kv: -kv[1])
    ) or "无"

    if template and template.strip():
        repl = {
            "range": range_text, "total": len(rows), "ok": ok, "fail": failed,
            "stopped": stopped, "busiest": busiest or "无",
            "detail": detail or "无", "kinds": kind_text,
        }
        filled = template
        for key, val in repl.items():
            filled = filled.replace("{" + key + "}", str(val))
        return filled

    lines = [f"🐑 SkySheep 运行周报 · {range_text}"]
    if not rows:
        lines.append("这一周没有定时任务或任务编排的运行记录。")
        return "\n".join(lines)
    lines.append(f"运行总数：{len(rows)}（{kind_text}）")
    lines.append(f"成功 {ok} / 失败 {failed} / 跳过或停止 {stopped}")
    lines.append("失败原因 Top 3：" if top_reasons else "失败原因：无")
    if detail:
        lines.extend(detail.splitlines())
    if busiest:
        lines.append("最忙的一天：" + busiest)
    if truncated:
        lines.append(f"（仅统计最近 {WEEKLY_RUN_LIMIT} 条运行，更早的请到工厂看板按日期筛）")
    return "\n".join(lines)


KIND_LABELS = {"cron": "定时任务", "pipeline_node": "流水线节点", "pipeline": "流水线"}


async def run_weekly_report_pass(
    store, mgr, *, now: float | None = None, push=None,
) -> dict:
    """周报的一轮检查：开关开、今天是配置的周几、已到点、本周没发过 → 推一次。

    防重发键是 ``weekly_last_sent_date``（与日报的 ``last_sent_date`` 分开记）：
    一天里循环每 60 秒扫一次，同一周只发一次。**无论送达与否都记日期**，理由
    同日报——周报不值得为不可达的地址刷一整天请求。
    """
    n = time.time() if now is None else float(now)
    state = load_state()
    if not state.get("weekly_enabled"):
        return {"sent": False, "reason": "disabled"}
    today = today_str(n)
    if state.get("weekly_last_sent_date") == today:
        return {"sent": False, "reason": "already_sent"}
    if not weekly_due(state, n):
        return {"sent": False, "reason": "not_due"}
    body = await build_weekly_report(store, now=n, template=state.get("weekly_template"))
    if push is None:
        from .automation import push_text_to_targets  # 局部导入避免模块环

        push = push_text_to_targets
    try:
        await push(mgr, body, what="运行周报")
    except Exception as e:  # noqa: BLE001 - 推送出口异常不影响记日期
        logger.warning("运行周报推送失败：%s", e)
    fresh = load_state()
    fresh["weekly_last_sent_date"] = today
    save_state(fresh)
    return {"sent": True, "reason": "ok", "body": body}
