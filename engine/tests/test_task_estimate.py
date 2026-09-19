"""任务耗时预估：估算器分档、历史实测校准、事件流接线（core/estimate.py）。

接手任务时按启发式 + 本项目近期实测给出预计区间，随 task_estimate 事件
先于本轮任何输出下发；前端显示「预计 X~Y 分钟」并对照已用时。
"""

from __future__ import annotations

from test_server import make_client, recv_until

from skysheep.core.estimate import estimate_task, format_range
from skysheep.messages import Message, TextBlock, ToolUseBlock


def _user(text: str) -> Message:
    return Message.user(text)


# ---- 估算器：档位判定 ----


def test_greeting_is_seconds():
    est = estimate_task("你好")
    assert est.level == "trivial"
    assert est.min_seconds <= est.max_seconds <= 60


def test_light_qa_stays_short():
    est = estimate_task("帮我把这段话翻译成英文")
    assert est.max_seconds <= 150
    assert "轻量问答" in est.basis


def test_crawl_task_lands_in_heavy():
    """爬虫/抓取类是多文件作业：落到分钟级档位。"""
    est = estimate_task("帮我写个爬虫抓取新闻网站并保存为 Excel")
    assert est.min_seconds >= 180
    assert est.level in ("heavy", "major")


def test_steps_and_scope_raise_estimate():
    plain = estimate_task("帮我重构这个模块")
    stepped = estimate_task(
        "帮我给整个项目做一次完整的代码重构：\n"
        "1. 先梳理模块依赖\n2. 重构核心逻辑\n3. 修复已知的并发 bug\n"
        "4. 补齐单元测试\n5. 更新文档"
    )
    assert stepped.max_seconds > plain.max_seconds
    assert "分步任务" in stepped.basis or "个步骤" in stepped.basis


def test_history_tool_calls_lift_a_nudge():
    """「继续」撞上多步历史：不应被寒暄档压成秒回。"""
    history = [_user("开始干活")]
    for i in range(5):
        history.append(Message.assistant([ToolUseBlock(id=f"t{i}", name="read_file", input={})]))
    est = estimate_task("继续", history=history)
    assert est.min_seconds > 8  # 纯寒暄档的下限
    assert "一句话应答" not in est.basis  # 历史把档拉回来后，依据不再自相矛盾
    assert "多步作业进行中" in est.basis


def test_tool_errors_note_in_basis():
    history = [_user("继续弄吧"),
               Message.tool_result("t0", "Traceback: boom", is_error=True)]
    est = estimate_task("接着修", history=history)
    assert "上一轮工具在报错" in est.basis


# ---- 估算器：历史实测校准 ----


def test_recent_samples_widen_range_upward():
    est = estimate_task("帮我写个小工具统计词频", recent=[600.0, 620.0, 590.0])
    # 实测中位数 10 分钟远超启发式上限 → 区间整体上移并注明依据
    assert est.min_seconds >= 200
    assert est.max_seconds >= 600
    assert "3 次实测" in est.basis


def test_too_few_samples_are_ignored():
    base = estimate_task("帮我写个小工具统计词频")
    est = estimate_task("帮我写个小工具统计词频", recent=[600.0, 700.0])
    assert (est.min_seconds, est.max_seconds) == (base.min_seconds, base.max_seconds)


def test_roundtable_scales_range():
    single = estimate_task("对比一下这几家模型的风格差异")
    multi = estimate_task("对比一下这几家模型的风格差异", members=3, debate_rounds=2)
    assert multi.max_seconds > single.max_seconds
    assert "圆桌 3 家" in multi.basis


def test_invariants_over_sample_inputs():
    texts = ["你好", "谢谢", "继续", "总结这段话", "写个函数", "帮我排查崩溃并重构整个模块"]
    for t in texts:
        est = estimate_task(t)
        assert 0 < est.min_seconds <= est.max_seconds
        assert est.level in ("trivial", "light", "normal", "moderate", "heavy", "major")


# ---- 区间与时长表述 ----


def test_format_range_units():
    assert format_range(8, 30) == "8~30 秒"
    assert format_range(45, 150) == "1~3 分钟"  # 混合单位统一成粗粒度
    assert format_range(600, 2400) == "10~40 分钟"
    assert format_range(3600, 7200) == "1~2 小时"


def test_format_range_single_value():
    assert format_range(5, 5) == "5 秒"
    assert format_range(180, 180) == "3 分钟"


# ---- 事件流接线 ----


def test_task_estimate_precedes_any_output(home):
    """接手任务：task_estimate 先于本轮任何输出事件到达，字段齐全。"""
    script = [[TextBlock(text="好的，这就开始")]]
    with make_client(home, script) as c, c.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        assert recv_until(ws, "b")["ok"]
        ws.send_json({"id": "c", "method": "chat.send",
                      "params": {"text": "帮我写个爬虫抓取新闻网站并保存为 Excel"}})
        ev = []
        frame = recv_until(ws, "c", ev)
        assert frame["ok"] and frame["result"]["done"]
        kinds = [e["event"] for e in ev]
        assert "task_estimate" in kinds
        # 先于第一个输出类事件（容忍连接上偶发的无关前置帧）
        first_out = min(
            i for i, k in enumerate(kinds)
            if k in ("turn_started", "text_delta", "assistant_message", "tool_call_started")
        )
        assert kinds.index("task_estimate") < first_out
        te = next(e for e in ev if e["event"] == "task_estimate")["data"]
        assert 0 < te["min_seconds"] <= te["max_seconds"]
        assert te["level"] in ("trivial", "light", "normal", "moderate", "heavy", "major")
        assert te["session_id"]
        assert "爬虫" not in te["basis"]  # basis 是信号说明，不是原文回显


def test_regen_turn_has_no_estimate(home):
    """重新生成轮不是接手新任务（text 为空）：不发预估。"""
    script = [[TextBlock(text="第一版")], [TextBlock(text="第二版")]]
    with make_client(home, script) as c, c.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        assert recv_until(ws, "b")["ok"]
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "你好"}})
        assert recv_until(ws, "c1")["ok"]
        ws.send_json({"id": "t1", "method": "session.truncate",
                      "params": {"mode": "regen"}})
        assert recv_until(ws, "t1")["ok"]
        ws.send_json({"id": "c2", "method": "chat.send",
                      "params": {"text": "", "regenerate": True}})
        ev = []
        frame = recv_until(ws, "c2", ev)
        assert frame["ok"]
        assert "task_estimate" not in [e["event"] for e in ev]
