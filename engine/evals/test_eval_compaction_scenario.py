"""评测基线 · 上下文压缩触发边界场景（fake provider 驱动真实 Agent 循环）。

场景 17/18：构造超长历史，钉住自动压缩的触发边界与压缩后果：
- 历史占用 ≤ 上限 × 触发比例（默认 0.9，留 10% 余量）：不压缩，本轮
  provider 调用直接就是主调用（压缩摘要调用不能抢先消费脚本）；
- 占用 > 上限 × 触发比例：轮首先压缩——历史里可压缩段被替换为一条带
  SUMMARY_OPEN_TAG 标记的 user 摘要消息，最近 keep_recent 条原样保留，
  事件流产出 compaction 事件（before/after 消息数、摘要字符数）。

token 估算按 core/context.py 的口径（CJK 0.7 token/字）：场景用整段中文
历史构造占用，正是当年「中文会话占用被低估约 2 倍、自动压缩触发过晚」
回归史覆盖的形态。
"""

from __future__ import annotations

from evals._harness import finished, make_agent, run_eval_turn
from skysheep.core.context import SUMMARY_OPEN_TAG
from skysheep.messages import Message, TextBlock
from skysheep.models.fake import FakeProvider


def _long_zh_history() -> list[Message]:
    """system + 四条各 200 个中文字的历史消息（估算占用远超下方的小上限）。"""
    filler = "这是一段用于撑大上下文占用的中文历史消息，内容本身并不重要。" * 3
    msgs = [Message.system("你是测试助手。")]
    for i in range(4):
        if i % 2 == 0:
            msgs.append(Message.user(f"第{i}轮提问：{filler}"))
        else:
            msgs.append(Message.assistant([TextBlock(text=f"第{i}轮回答：{filler}")]))
    return msgs


async def test_eval_compaction_not_triggered_below_threshold(home):
    """场景 17（压缩触发边界 · 低于阈值侧）：占用 ≤ 上限 × 触发比例不压缩。"""
    proj = home / "proj"
    history = _long_zh_history()[:3]  # system + 一问一答：占用压在小上限之下
    provider = FakeProvider([[TextBlock(text="好的，继续。")]])
    # 上限 5000 × 0.9 = 4500 token 阈值；三条短消息远达不到
    agent = make_agent(provider, proj, context_limit_tokens=5000,
                       compaction_keep_recent=2)
    agent.load_history(history)
    used = agent.used_context_tokens()
    assert used <= 5000 * 0.9, f"前置不成立：历史占用 {used} 应低于触发线"

    events = await run_eval_turn(agent, "继续")
    assert not any(e.kind == "compaction" for e in events), "低于触发线不得压缩"
    assert provider.calls[0][-1].role == "user" and provider.calls[0][-1].text == "继续", (
        "第一次 provider 调用就是主调用（没有摘要调用抢跑）"
    )
    assert events[-1].kind == "turn_finished" and events[-1].stop_reason == "end_turn"
    # 历史完整保留：没有被摘要替换的痕迹
    assert not any(
        m.role == "user" and m.text.lstrip().startswith(SUMMARY_OPEN_TAG)
        for m in agent.history
    )


async def test_eval_compaction_triggers_on_overlong_history(home):
    """场景 18（压缩触发边界 · 超阈值侧 + 压缩后果）：超线先压缩再跑主循环。

    用小上限（60 token × 0.9 = 54）逼出触发：200 字 × 4 条的中文历史估算
    占用约 560 token，远超触发线。钉住的行为：
    - 事件流出现 compaction（before/after 消息数、摘要字符数 > 0）；
    - provider 的第一次调用是压缩摘要调用（transcript 里带着被压缩的旧消息，
     COMPACT_USER_TEMPLATE 的分段标记可见），第二次才是主调用；
    - 压缩后历史 = [system, 摘要 user 消息（SUMMARY_OPEN_TAG 开头、含摘要
      正文与「以上是此前对话的摘要」注记）] + 保留的最近 keep_recent 条；
    - 主循环照常完成（turn_finished=end_turn，最终回答进入历史）。
    """
    proj = home / "proj"
    provider = FakeProvider([
        [TextBlock(text="摘要：用户此前问了两个问题并得到回答，任务仍在进行。")],
        [TextBlock(text="收到，接着上一轮继续。")],
    ])
    agent = make_agent(provider, proj, context_limit_tokens=60,
                       compaction_keep_recent=2)
    history = _long_zh_history()
    agent.load_history(history)
    used = agent.used_context_tokens()
    assert used > 60 * 0.9, f"前置不成立：历史占用 {used} 应超过触发线"

    events = await run_eval_turn(agent, "继续")
    compactions = [e for e in events if e.kind == "compaction"]
    assert len(compactions) == 1, "轮首压缩恰好发生一次"
    ev = compactions[0]
    assert ev.before_messages == 6 and ev.after_messages == 4, (
        "压缩把 3 条旧消息换成 1 条摘要（6 = system + 4 条历史 + 本轮输入）"
    )
    assert ev.summary_chars > 0

    # 第一次 provider 调用 = 摘要调用：带 CONVERSATION SEGMENT 模板与旧消息正文
    summary_call = provider.calls[0]
    assert "CONVERSATION SEGMENT" in summary_call[-1].text
    assert "第0轮提问" in summary_call[-1].text, "被压缩的旧消息进入摘要稿"
    # 第二次调用 = 主调用：历史已是压缩后的形态
    main_call = provider.calls[1]
    assert main_call[0].role == "system"
    assert main_call[1].role == "user" and main_call[1].text.startswith(SUMMARY_OPEN_TAG)
    assert main_call[-1].text == "继续"

    # 压缩后的历史形态：system + 摘要 + 最近 2 条（+ 本轮新产出的最终回答）
    assert len(agent.history) == 5
    summary_msg = agent.history[1]
    assert summary_msg.role == "user" and summary_msg.text.startswith(SUMMARY_OPEN_TAG)
    assert "摘要：用户此前问了两个问题" in summary_msg.text, "摘要正文进入注入消息"
    assert "以上是此前对话的摘要" in summary_msg.text, "带「这是摘要」注记"
    assert agent.history[2].text.startswith("第2轮") or agent.history[2].text.startswith("第3轮"), (
        "最近 keep_recent 条原样保留"
    )
    # 轮照常完成，最终回答进历史
    assert events[-1].kind == "turn_finished" and events[-1].stop_reason == "end_turn"
    assert any(
        m.role == "assistant" and m.text == "收到，接着上一轮继续。" for m in agent.history
    )
    done = finished(events)
    assert not done, "本轮没有工具调用"
