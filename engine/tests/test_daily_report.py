"""每日运行日报：汇总内容、防重发、开关关不推、状态文件原子落盘。

日报的推送目标复用既有渠道推送逻辑（push_text_to_targets），这里用替身
push 注入验证「什么时候推、推什么、发过记日期」；端到端经真实 webhook
渠道送达的用例在 test_webhook_channel.py。状态文件走 SKYSHEEP_HOME 隔离。
"""

from __future__ import annotations

import json
import time
from datetime import UTC

import pytest

from skysheep.server.backend_parts import daily_report as dr
from skysheep.server.backend_parts.automation import AutomationMixin
from skysheep.server.backend_parts.daily_report import (
    build_daily_report,
    load_state,
    parse_hhmm,
    run_daily_report_pass,
    save_state,
)

# ---- 种子数据的小件 ----


def _day_window(now: float | None = None) -> tuple[float, float]:
    return dr._day_bounds(time.time() if now is None else now)


async def _seed_cron(store, name: str, status: str, run_at: float, result: str = "") -> dict:
    project = await store.get_or_create_project("/tmp/daily-proj")
    t = await store.add_cron_task(project.id, name, "干活", "interval", interval_minutes=30)
    return await store.update_cron_task(
        t["id"], last_run_at=run_at, last_status=status, last_result=result,
    )


async def _seed_pipeline(
    store, name: str, status: str, finished_at: float,
    node_specs: list[tuple[str, str, str]] | None = None,
) -> dict:
    """node_specs: [(title, status, last_error), ...]，缺省一个 done 节点。"""
    project = await store.get_or_create_project("/tmp/daily-proj")
    specs = node_specs or [("节点一", "done", "")]
    pipe = await store.add_pipeline(
        project.id, name,
        nodes=[{"title": t, "prompt": "做事"} for t, _s, _e in specs],
    )
    for node, (_title, n_status, err) in zip(pipe["nodes"], specs, strict=True):
        await store.update_pipeline_node(
            node["id"], status=n_status, last_error=err,
            result="" if err else "产出",
            finished_at=finished_at if n_status != "blocked" else 0,
        )
    return await store.update_pipeline(pipe["id"], status=status, finished_at=finished_at)


class _RecordingPush:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, mgr, body: str, *, what: str = "") -> int:
        self.calls.append((what, body))
        return 1


# ---- 汇总内容 ----


async def test_build_report_counts_and_failure_detail(store):
    start, _end = _day_window()
    today_noon = start + 12 * 3600
    yesterday = start - 3600
    await _seed_cron(store, "扫 TODO", "ok", today_noon, "完成 3 件事")
    await _seed_cron(store, "抓行情", "error", today_noon + 60, "连接超时")
    await _seed_cron(store, "昨天的失败", "error", yesterday, "不应出现在今天的日报")
    await _seed_pipeline(store, "周报生成", "done", today_noon)
    await _seed_pipeline(store, "发布流水", "failed", today_noon + 120,
                         [("构建", "done", ""), ("审查", "error", "节点没有产出"),
                          ("上传", "error", "磁盘满")])
    await _seed_pipeline(store, "手工停掉", "cancelled", today_noon + 180)

    body = await build_daily_report(store)

    assert f"运行日报 · {time.strftime('%Y-%m-%d')}" in body
    assert "定时任务：成功 1 / 失败 1" in body
    assert "任务编排：完成 1 / 失败 1 / 已停止 1" in body
    assert "失败明细：" in body
    # 失败明细：点名任务与原因；昨天的失败与已停止流水线不进明细
    assert "· 定时任务「抓行情」失败：连接超时" in body
    assert "· 流水线「发布流水」失败，2 个节点未成功：审查——节点没有产出" in body
    assert "另有 1 个节点未成功" in body
    assert "昨天的失败" not in body
    assert "手工停掉" not in body
    assert "扫 TODO" not in body  # 成功的不进明细


async def test_build_report_empty_day(store):
    body = await build_daily_report(store)
    assert "今日没有定时任务或任务编排的运行记录" in body
    assert "失败明细" not in body


async def test_build_report_all_success_shows_no_detail(store):
    start, _end = _day_window()
    await _seed_cron(store, "顺利", "ok", start + 3600, "完成")
    await _seed_cron(store, "无产出", "empty", start + 3660)
    body = await build_daily_report(store)
    assert "定时任务：成功 2 / 失败 0" in body
    assert "失败明细：无" in body


async def test_build_report_clips_long_error(store):
    start, _end = _day_window()
    await _seed_cron(store, "长原因", "error", start + 3600, "错" * 500)
    body = await build_daily_report(store)
    assert "…（已截断）" in body


# ---- 防重发 / 开关 / 到点判定 ----


async def test_pass_disabled_does_not_push_or_mark(store, home):
    push = _RecordingPush()
    out = await run_daily_report_pass(store, None, push=push)
    assert out == {"sent": False, "reason": "disabled"}
    assert push.calls == []
    assert not dr.state_path().exists(), "开关关：不该写状态文件"
    assert load_state()["last_sent_date"] == ""


async def test_pass_sends_once_per_day_and_records_date(store, home):
    start, _end = _day_window()
    now = start + 3600
    await _seed_cron(store, "扫 TODO", "ok", now, "完成")
    save_state({"enabled": True, "time": "00:00"})
    push = _RecordingPush()

    first = await run_daily_report_pass(store, None, now=now, push=push)
    assert first["sent"] is True and first["reason"] == "ok"
    assert len(push.calls) == 1
    what, body = push.calls[0]
    assert what == "运行日报"
    # 成功的只进计数（日报要一眼看完），失败明细才点名
    assert "定时任务：成功 1 / 失败 0" in body and "扫 TODO" not in body

    # 状态文件里记下了今天的日期（防重发键）
    state = load_state()
    assert state["last_sent_date"] == dr.today_str(now)

    # 同一天再跑：不再发
    second = await run_daily_report_pass(store, None, now=now + 60, push=push)
    assert second == {"sent": False, "reason": "already_sent"}
    assert len(push.calls) == 1

    # 换开关状态也不影响防重发：先关再开，日期仍在，当天依旧不发
    save_state({**state, "enabled": False})
    save_state({**load_state(), "enabled": True})
    third = await run_daily_report_pass(store, None, now=now + 120, push=push)
    assert third["reason"] == "already_sent"
    assert len(push.calls) == 1


async def test_pass_skips_before_report_time(store, home):
    start, _end = _day_window()
    now = start + 3600  # 当天 01:00
    save_state({"enabled": True, "time": "23:59"})
    push = _RecordingPush()
    out = await run_daily_report_pass(store, None, now=now, push=push)
    assert out == {"sent": False, "reason": "not_due"}
    assert push.calls == []
    assert load_state()["last_sent_date"] == "", "没到点不发也不记日期"


async def test_pass_records_date_even_when_delivery_fails(store, home):
    """推送出口抛异常也只记一次：日报每天只尝试一次，不做全天重试。"""
    now = _day_window()[0] + 3600
    save_state({"enabled": True, "time": "00:00"})

    async def bad_push(mgr, body, *, what=""):
        raise RuntimeError("全渠道不可达")

    out = await run_daily_report_pass(store, None, now=now, push=bad_push)
    assert out["sent"] is True
    assert load_state()["last_sent_date"] == dr.today_str(now)


async def test_pass_keeps_concurrent_enabled_change(store, home):
    """记日期前重读状态合并：并发保存的开关改动不被覆盖。"""
    now = _day_window()[0] + 3600
    save_state({"enabled": True, "time": "00:00"})

    async def save_concurrently(mgr, body, *, what=""):
        save_state({**load_state(), "time": "20:30"})  # 模拟推送期间用户改了时刻

    out = await run_daily_report_pass(store, None, now=now, push=save_concurrently)
    assert out["sent"] is True
    state = load_state()
    assert state["last_sent_date"] == dr.today_str(now)
    assert state["time"] == "20:30", "并发改动被覆盖就丢用户输入了"


# ---- 状态文件 ----


def test_state_defaults_disabled_and_roundtrip(home):
    assert not dr.state_path().exists()
    st = load_state()
    assert st == {
        "enabled": False, "time": dr.DEFAULT_REPORT_TIME, "last_sent_date": "",
        "weekly_enabled": False, "weekly_time": dr.DEFAULT_REPORT_TIME,
        "weekly_weekday": 0, "weekly_last_sent_date": "",
    }

    save_state({"enabled": True, "time": "8:05", "last_sent_date": "2026-10-01"})
    back = load_state()
    assert back["enabled"] is True and back["time"] == "8:05"
    assert back["last_sent_date"] == "2026-10-01"
    # 周报默认值随同一份状态落盘（未配置 = 关 + 周一 09:00）
    assert back["weekly_enabled"] is False and back["weekly_weekday"] == 0
    raw = json.loads(dr.state_path().read_text(encoding="utf-8"))
    assert raw["enabled"] is True


def test_state_tolerates_garbage(home):
    dr.state_path().parent.mkdir(parents=True, exist_ok=True)
    dr.state_path().write_text("不是 JSON", encoding="utf-8")
    st = load_state()
    assert st["enabled"] is False
    dr.state_path().write_text('{"enabled": "yes", "time": "99:99"}', encoding="utf-8")
    st = load_state()
    assert st["enabled"] is False and st["time"] == dr.DEFAULT_REPORT_TIME


def test_state_unknown_fields_dropped(home):
    save_state({"enabled": True, "time": "07:00", "hacker": "x", "last_sent_date": "d"})
    back = load_state()
    assert back["enabled"] is True and back["time"] == "07:00"
    assert back["last_sent_date"] == "d"
    assert "hacker" not in back, "未知字段不落盘"


@pytest.mark.parametrize("raw,expect", [
    ("09:00", (9, 0)), ("9:05", (9, 5)), ("00:00", (0, 0)), ("23:59", (23, 59)),
    ("24:00", None), ("9", None), ("", None), ("abc", None), ("09:60", None),
    (None, None), ("  07:30  ", (7, 30)),
])
def test_parse_hhmm(raw, expect):
    assert parse_hhmm(raw) == expect


# ---- mixin 上的设置读写（接线阶段将挂成 WS 方法） ----


class _Host:
    """只要混入的两个方法，不背整个 backend。"""

    daily_report_status = AutomationMixin.daily_report_status
    daily_report_save = AutomationMixin.daily_report_save


async def test_mixin_daily_report_save_and_status(home):
    host = _Host()
    out = await host.daily_report_save({"enabled": True, "time": "07:30"})
    assert out["enabled"] is True and out["time"] == "07:30"
    # 只改开关，时刻保持
    out = await host.daily_report_save({"enabled": False})
    assert out["enabled"] is False and out["time"] == "07:30"
    assert out["last_sent_date"] == ""
    # 周报三件套可以单独改，互不影响日报那组
    out = await host.daily_report_save({
        "weekly_enabled": True, "weekly_weekday": 5, "weekly_time": "20:00"})
    assert out["weekly_enabled"] is True
    assert out["weekly_weekday"] == 5 and out["weekly_time"] == "20:00"
    assert out["enabled"] is False, "改周报不该动日报开关"
    with pytest.raises(RuntimeError):
        await host.daily_report_save({"weekly_weekday": 9})
    with pytest.raises(RuntimeError):
        await host.daily_report_save({"weekly_time": "25:00"})


async def test_mixin_daily_report_rejects_bad_time(home):
    host = _Host()
    with pytest.raises(RuntimeError):
        await host.daily_report_save({"time": "25:00"})
    with pytest.raises(RuntimeError):
        await host.daily_report_save({"time": "九点"})


async def test_daily_report_pass_via_default_push_path(store, home, monkeypatch):
    """缺省 push 时接既有推送目标逻辑：mgr 无目标渠道 → 静默、仍记日期。"""
    from skysheep.server.backend_parts.automation import push_text_to_targets

    seen = []

    async def fake_push(mgr, body, *, what="定时任务摘要"):
        seen.append((mgr, body))
        return await push_text_to_targets(mgr, body, what=what)

    monkeypatch.setattr(
        "skysheep.server.backend_parts.automation.push_text_to_targets", fake_push,
    )
    save_state({"enabled": True, "time": "00:00"})
    out = await run_daily_report_pass(store, None, now=_day_window()[0] + 3600)
    assert out["sent"] is True
    assert len(seen) == 1 and "运行日报" in seen[0][1]
    assert load_state()["last_sent_date"] == dr.today_str(_day_window()[0] + 3600)


def test_day_bounds_end_at_next_local_midnight_across_dst(monkeypatch):
    """DST 回拨日本地天长 25 小时：窗口终点是次日本地零点，不按固定 86400 秒提前收口。

    本地时区在测试里换不了（Windows 没有 tzset），用分片固定偏移的 datetime
    替身模拟「-4 → -5」的秋季回拨：回拨日 23:30 的终态必须落在当天窗口内
    （固定 86400 外推的窗口提前 1 小时收口，会把它漏给明天）；无切换日照常
    24 小时。日报与运行总览（run_center_summary 经 today_bounds）共用取窗。
    """
    from datetime import datetime

    utc = UTC
    flip = datetime(2026, 11, 1, 6, 0, tzinfo=utc).timestamp()  # 02:00 EDT→01:00 EST

    def offset_at(ts: float) -> int:
        return -4 * 3600 if ts < flip else -5 * 3600

    class _FakeDT(datetime):
        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            base = datetime.fromtimestamp(ts + offset_at(ts), tz=utc)
            return cls(base.year, base.month, base.day, base.hour, base.minute)

        def timestamp(self):
            naive = datetime.timestamp(self.replace(tzinfo=utc))  # 基类实现，防自递归
            for off in (-4 * 3600, -5 * 3600):
                ts = naive - off
                if _FakeDT.fromtimestamp(ts) == self:
                    return ts
            raise ValueError(f"无法把本地时间映射回 epoch：{self}")

    monkeypatch.setattr(dr, "datetime", _FakeDT)

    # 回拨日（本地 2026-11-01）当地正午 = 17:00 UTC（-5）
    noon = datetime(2026, 11, 1, 17, 0, tzinfo=utc).timestamp()
    start, end = dr._day_bounds(noon)
    assert end - start == 25 * 3600, "回拨日本地天长 25 小时，窗口到次日本地零点"
    # 当天本地 23:30（EST = 次日 04:30 UTC）：固定 86400 外推的窗口已提前收口
    late_evening = datetime(2026, 11, 2, 4, 30, tzinfo=utc).timestamp()
    assert start <= late_evening < end, "回拨日 23:30 的终态属于当天窗口"
    assert late_evening > start + 86400.0, "（回归对照）固定 86400 秒外推确实漏掉它"

    # 对照：无切换日照常 24 小时窗口
    mid = datetime(2026, 11, 15, 17, 0, tzinfo=utc).timestamp()
    s2, e2 = dr._day_bounds(mid)
    assert e2 - s2 == 24 * 3600


# ---- P10 模板自定义 ----


async def test_build_report_template_placeholders(store):
    start, _end = _day_window()
    now = start + 3600
    await _seed_cron(store, "备份额", "ok", now)
    await _seed_cron(store, "巡检", "error", now, result="连不上")
    await _seed_pipeline(store, "夜批", "failed", now)
    body = await build_daily_report(
        store,
        template=(
            "日报 {date}\n定时 {cron_ok}/{cron_fail}"
            " 编排 {pipe_done}/{pipe_fail}/{pipe_stop}\n明细:\n{detail}"
        ),
    )
    assert "定时 1/1" in body
    assert "编排 0/1/0" in body
    assert "巡检" in body and "连不上" in body  # {detail} 展开失败行
    assert "{" not in body  # 已知占位符全部替换


async def test_build_report_template_empty_day_keeps_unknown_placeholder(store):
    body = await build_daily_report(store, template="今日 {cron_ok}·{detail}·{unknown}")
    assert "今日 0·无·{unknown}" in body  # 无失败 {detail}=「无」；未知占位原样保留


async def test_pass_uses_saved_template(store, home):
    start, _end = _day_window()
    now = start + 3600
    await _seed_cron(store, "扫 TODO", "ok", now)
    save_state({"enabled": True, "time": "00:00", "template": "模板版 {cron_ok} 项"})
    push = _RecordingPush()
    out = await run_daily_report_pass(store, None, now=now, push=push)
    assert out["sent"] is True
    assert out["body"] == "模板版 1 项"
    assert "🐑" not in out["body"]  # 模板模式下不再拼内置头


async def test_daily_report_save_template_validation(home):
    """save 路径：合法收、空白恢复内置（删键）、超长显式报错不截断。"""
    save_state({"enabled": True, "time": "09:00"})
    b = AutomationMixin.__new__(AutomationMixin)  # 这两个方法不摸后端属性，裸实例可调
    r = await AutomationMixin.daily_report_save(b, {"template": "自定义 {cron_fail}"})
    assert r["template"] == "自定义 {cron_fail}"
    r = await AutomationMixin.daily_report_save(b, {"template": "   "})
    assert "template" not in r
    with pytest.raises(RuntimeError):
        await AutomationMixin.daily_report_save(b, {"template": "x" * 2001})
    # 模板操作不误伤已有开关
    assert load_state()["enabled"] is True


# ---- 周报（P10 剩余半边）：7 天窗口 / 周几时刻 / 防重发 ----


def test_weekly_defaults_off_and_state_roundtrip(home):
    st = load_state()
    assert st["weekly_enabled"] is False
    assert st["weekly_weekday"] == 0 and st["weekly_time"] == "09:00"
    st.update({
        "weekly_enabled": True, "weekly_weekday": 3, "weekly_time": "10:30",
        "weekly_template": "总 {total} 次，失败 {fail}",
    })
    save_state(st)
    back = load_state()
    assert back["weekly_enabled"] is True
    assert back["weekly_weekday"] == 3 and back["weekly_time"] == "10:30"
    assert back["weekly_template"] == "总 {total} 次，失败 {fail}"
    # 周几只认 0..6，其余回落周一（手改状态文件的兜底）
    bad = load_state()
    bad["weekly_weekday"] = 9
    save_state(bad)
    assert load_state()["weekly_weekday"] == 0


def test_weekly_due_matches_weekday_and_time(home):
    from datetime import datetime

    from skysheep.server.backend_parts.daily_report import weekly_due

    # 2026-10-28 是周三（weekday=2）
    noon = datetime(2026, 10, 28, 12, 0).timestamp()
    state = {"weekly_weekday": 2, "weekly_time": "09:00"}
    assert weekly_due(state, noon) is True
    assert weekly_due({"weekly_weekday": 1, "weekly_time": "09:00"}, noon) is False
    # 到了周几但没到点：不发
    assert weekly_due({"weekly_weekday": 2, "weekly_time": "18:00"}, noon) is False


def test_week_bounds_is_seven_local_days(home):
    from datetime import datetime

    from skysheep.server.backend_parts.daily_report import _week_bounds

    now = datetime(2026, 10, 28, 12, 0).timestamp()
    start, end = _week_bounds(now)
    assert datetime.fromtimestamp(start) == datetime(2026, 10, 22)
    assert datetime.fromtimestamp(end) == datetime(2026, 10, 29)


async def test_build_weekly_report_counts_from_run_ledger(store):
    from skysheep.server.backend_parts.daily_report import build_weekly_report

    now = time.time()
    for i, (kind, status, outcome, err) in enumerate([
        ("cron", "done", "ok", ""),
        ("cron", "error", "failed", "凭据过期：401"),
        ("cron", "error", "failed", "凭据过期：401"),
        ("pipeline_node", "done", "ok", ""),
        ("pipeline_node", "skipped", "", ""),
    ]):
        run = await store.add_automation_run(
            kind=kind, task_id=i + 1, status=status, started_at=now - i * 3600,
        )
        await store.finish_automation_run(
            run["id"], status=status, outcome=outcome, error=err, finished_at=now,
        )
    body = await build_weekly_report(store, now=now)
    assert "运行周报" in body
    assert "运行总数：5" in body
    assert "成功 2 / 失败 2 / 跳过或停止 1" in body
    assert "2 次：凭据过期：401" in body
    assert "定时任务 3" in body
    # 模板直替
    tpl = await build_weekly_report(store, now=now, template="{range}｜{total}/{ok}/{fail}/{stopped}")
    assert tpl.startswith(dr.today_str(now - 6 * 86400)[:4])  # 以年份开头
    assert tpl.count("/") == 3 and tpl.endswith("/2/1")


async def test_weekly_pass_respects_schedule_and_records_once(store, home):
    from skysheep.server.backend_parts.daily_report import run_weekly_report_pass

    push = _RecordingPush()
    state = load_state()
    state.update({"weekly_enabled": True, "weekly_weekday": 0, "weekly_time": "09:00"})
    save_state(state)
    # 用「今天是不是周一」决定断言方向：周一 10:00 → 发；其它日子 → not_due
    from datetime import datetime

    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    is_monday = now.weekday() == 0
    r1 = await run_weekly_report_pass(store, None, now=now.timestamp(), push=push)
    if is_monday:
        assert r1["sent"] is True and push.calls[0][0] == "运行周报"
        r2 = await run_weekly_report_pass(store, None, now=now.timestamp(), push=push)
        assert r2 == {"sent": False, "reason": "already_sent"}
        assert len(push.calls) == 1
    else:
        assert r1 == {"sent": False, "reason": "not_due"}
        # 换到今天对应的周几再试一次
        state = load_state()
        state["weekly_weekday"] = now.weekday()
        save_state(state)
        r3 = await run_weekly_report_pass(store, None, now=now.timestamp(), push=push)
        assert r3["sent"] is True


async def test_weekly_pass_disabled_by_default(store, home):
    from skysheep.server.backend_parts.daily_report import run_weekly_report_pass

    push = _RecordingPush()
    r = await run_weekly_report_pass(store, None, now=time.time(), push=push)
    assert r == {"sent": False, "reason": "disabled"}
    assert push.calls == []


# ---- 周报（P10 剩余半边）：7 天窗口 / 周几时刻 / 防重发 ----

