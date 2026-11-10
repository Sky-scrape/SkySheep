"""场景 34：轮指标预算（评测指标化）。

回归史：评测基线此前只有「过/不过」——一次场景红/绿都看不出这一轮多贵多慢。
本场景用真实 Agent 循环跑两轮（正常只读工具 / 写入被拒绝），把
``TurnMetrics`` 的每一项与预算钉住，并演示 ``metrics`` 夹具把明细写进
``evals/.metrics/last-run.jsonl``。

指标全部取自事件流（usage / tool_call_finished / permission_* / turn_finished），
不依赖模型文本，因此可以稳定断言。
"""

from __future__ import annotations

from evals._harness import make_agent, run_eval_turn
from evals._metrics import TurnMetrics, assert_metrics
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider


async def test_turn_metrics_for_auto_allowed_readonly_tool(home, metrics):
    proj = home / "proj"
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="list_dir", input={"path": "."})],
        [TextBlock(text="目录看完了")],
    ])
    agent = make_agent(provider, proj)
    events = await run_eval_turn(agent, "看看目录里有什么")

    m = TurnMetrics.from_events(events)
    assert m.tool_calls == 1 and m.tool_errors == 0
    assert m.permission_requests == 0  # 只读工具自动放行
    assert m.stop_reason == "end_turn"
    assert m.iterations == 2  # 一次工具轮 + 一次收尾
    assert m.total_tokens == 36  # FakeProvider 每次 11 in + 7 out
    assert m.text_chars > 0
    assert_metrics(
        m, max_tool_calls=1, max_permission_requests=0,
        max_total_tokens=100, max_duration_ms=15_000, max_model_retries=0,
    )
    metrics.append(m)  # 随用例结果落盘（conftest 的指标插件）


async def test_turn_metrics_for_denied_write(home, metrics):
    """被拒绝的写入同样计入工具调用与错误，并留下一次确认记录。"""
    proj = home / "proj"
    provider = FakeProvider([
        [ToolUseBlock(
            id="w1", name="write_file",
            input={"path": "out.txt", "content": "hello"},
        )],
        [TextBlock(text="写入被拒绝了")],
    ])
    agent = make_agent(provider, proj)
    events = await run_eval_turn(agent, "写一个文件", decide=lambda _ev: "deny")

    m = TurnMetrics.from_events(events)
    assert m.permission_requests == 1 and m.permission_denied == 1
    assert m.tool_calls == 1 and m.tool_errors == 1
    assert not (proj / "out.txt").exists()  # fail-closed：什么都没写
    assert m.stop_reason == "end_turn"
    assert_metrics(m, max_permission_requests=1, max_tool_calls=1)
    metrics.append(m)
