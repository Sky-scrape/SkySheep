"""评测基线 · 渠道权限门行为场景（fake provider 驱动真实 Agent 循环）。

四个种子场景，各自对应渠道遥控行为的既有审查结论（出处见各用例 docstring）：

12. approve_enabled=False（默认无人值守档）：写操作立刻拒绝，不挂起、不推卡、
    不落盘；预授权名单内的写工具照常放行（对照）；
13. 审批卡路径：卡片推出后 Agent 挂起等回复；决定只认发起人；「总是允许」
    在渠道端降级为单次放行、不沉淀规则；
14. 审批超时自动拒绝（超时兜底是硬要求，不是体验优化）；
15. 推卡片失败退化为拒绝（通道断了也不能把写操作挂死或误放行）。

ChannelGate 是产品真实权限门（channels/gate.py），经 _harness.make_agent
驱动真实 Agent 循环；notify 回调记录「卡片已推送」这一刻，卡片文案的格式化
属渠道适配层（manager 侧），已有单测覆盖，不在评测重复。
"""

from __future__ import annotations

import asyncio

from evals._harness import finished, make_agent, requests, resolved, run_eval_turn
from skysheep.channels.gate import ChannelGate
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider


async def test_eval_channel_gate_unattended_write_denied_without_prompt(home):
    """场景 12（渠道无人值守写拒绝）：approve 关（默认）时写操作立刻拒绝。

    渠道会话可能在后端无人盯屏时运行——默认 PermissionGate 会产出
    PermissionRequest 并无限期挂起等前端决策，渠道场景下没有前端，Agent 会
    永久卡死。钉住的行为：写文件立刻得到「已拒绝」，轮照常收尾
    （turn_finished=end_turn），文件不落盘，审批卡一次都没推。
    对照：预授权名单（allowed）里的写工具无人值守照常放行并落盘——拒绝
    不是「渠道什么都不能干」，而是「没授权的写绝不静默执行」。
    """
    proj = home / "proj"
    gate = ChannelGate(allowed=[], approve_enabled=False, working_dir=proj)
    pushed: list = []

    async def notify(pending) -> None:
        pushed.append(pending)  # 推卡片入口：无人值守档不该走到

    gate.notify = notify
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="没写成也说句话")],
    ])
    agent = make_agent(provider, proj, gate=gate)
    events = await run_eval_turn(agent, "偷偷写个文件")  # 不传 decide = deny（fail-closed）

    reqs = requests(events)
    assert [e.tool_name for e in reqs] == ["write_file"], "写入必须过门（产生权限请求）"
    assert resolved(events)[0].decision == "deny", "无人值守档立刻拒绝，不挂起"
    assert not pushed, "approve 关闭时不得推送审批卡"
    assert not (proj / "secret.txt").exists(), "拒绝后不落盘"
    done = finished(events)
    assert done["w1"].is_error and "denied" in done["w1"].preview.lower()
    assert events[-1].kind == "turn_finished" and events[-1].stop_reason == "end_turn", (
        "拒绝后 Agent 继续收尾，轮不挂死"
    )

    # 对照：名单放行——同一无人值守档位下，预授权的写照常执行
    gate2 = ChannelGate(allowed=["write_file"], approve_enabled=False, working_dir=proj)
    provider2 = FakeProvider([
        [ToolUseBlock(id="w2", name="write_file",
                      input={"path": "allowed.txt", "content": "ok"})],
        [TextBlock(text="写好了")],
    ])
    agent2 = make_agent(provider2, proj, gate=gate2)
    events2 = await run_eval_turn(agent2, "写预授权的文件")
    assert requests(events2) == [], "名单内的写不弹确认"
    assert (proj / "allowed.txt").read_text(encoding="utf-8") == "ok"


async def test_eval_channel_gate_approval_card_actor_and_always_downgrade(home):
    """场景 13（审批卡路径）：推卡挂起 → 只认发起人 → 「总是允许」降级单次。

    - 卡片推出（notify 回调收到待决项）后 Agent 挂起等聊天窗口回复；
    - submit_latest 带 actor 校验：非发起人的回复不生效（群聊里别人一句
      allow 不能替发起人批准，审查 S-08/P3-17）；
    - 渠道端没有 allow always：提交「总是允许」被降级为单次放行——白名单
      规则是持久化的，聊天窗口不得成为沉淀永久规则的通道（工具执行后
      gate 里不新增任何规则）。
    """
    proj = home / "proj"
    cards: list = []

    async def notify(pending) -> None:
        cards.append(pending)  # 渠道把确认卡推给聊天窗口（此回调即推送时刻）

    gate = ChannelGate(allowed=[], approve_enabled=True, approve_timeout=10,
                       notify=notify, working_dir=proj)
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "chan.txt", "content": "hi"})],
        [TextBlock(text="写好了")],
    ])
    agent = make_agent(provider, proj, gate=gate)
    # 宿主每轮开始前写入发起人与所在聊天（channels.py 的 channel_run 同款动作）
    gate.turn_actor = "user-1"
    gate.turn_chat_id = "chat-9"

    evs: list = []

    async def collect(agen):
        async for ev in agen:
            evs.append(ev)

    task = asyncio.create_task(collect(agent.run_turn("把这句话写进文件")))
    try:
        for _ in range(500):
            if gate.waiting and cards:
                break
            await asyncio.sleep(0.01)
        assert cards and cards[0].tool_name == "write_file", "审批卡已推出（推送时刻）"
        assert gate.waiting, "推卡后 Agent 挂起等回复"

        assert gate.submit_latest("allow_once", actor="user-2") is None, (
            "非发起人的决定不生效"
        )
        assert gate.waiting, "被冒名的决策不能消掉待决项"
        rid = next(iter(gate.waiting))
        assert gate.submit_latest("allow_always", actor="user-1") == rid, (
            "发起人回复命中待决项"
        )
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert resolved(evs)[0].decision == "allow_once", "「总是允许」被渠道端降级为单次"
    assert not gate._project_rules and not gate.session_rules, (
        "聊天窗口的回复不得沉淀任何持久白名单规则"
    )
    assert not gate.waiting, "决策后待决项清空"
    assert (proj / "chan.txt").read_text(encoding="utf-8") == "hi", "单次放行后落盘一次"
    assert evs[-1].kind == "turn_finished" and evs[-1].stop_reason == "end_turn"


async def test_eval_channel_gate_approval_timeout_auto_denies(home):
    """场景 14（审批超时兜底）：超时无人回复自动拒绝。

    respond_permission 对失效的 request_id 返回 False——不超时就会永久挂起，
    所以超时自动 DENY 是硬要求。钉住的行为：到点自动拒绝、轮照常收尾、
    文件不落盘、待决项清空。
    """
    proj = home / "proj"
    pushed: list = []

    async def notify(pending) -> None:
        pushed.append(pending)

    gate = ChannelGate(allowed=[], approve_enabled=True, approve_timeout=1,
                       notify=notify, working_dir=proj)
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "late.txt", "content": "x"})],
        [TextBlock(text="没人理就先不写")],
    ])
    agent = make_agent(provider, proj, gate=gate)
    gate.turn_actor = "user-1"
    gate.turn_chat_id = "chat-9"

    events = []

    async def drain() -> None:
        async for ev in agent.run_turn("写个文件"):
            events.append(ev)

    task = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(task, timeout=15)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert pushed, "卡片先推出"
    assert resolved(events)[0].decision == "deny", "超时自动拒绝"
    assert not gate.waiting, "超时后待决项清空（respond 再投递不进来）"
    assert not (proj / "late.txt").exists(), "超时拒绝后不落盘"
    assert events[-1].kind == "turn_finished" and events[-1].stop_reason == "end_turn"


async def test_eval_channel_gate_card_push_failure_degrades_to_deny(home):
    """场景 15（推卡失败退化成拒绝）：notify 抛异常时不能挂住循环、更不能放行。

    推卡片失败（通道断了、适配器崩了）按拒绝处理：待决项立刻落定为 deny、
    从 waiting 摘除，Agent 拿到「已拒绝」继续收尾。fail-closed——审批通道
    不健康时宁可做不成事，不能把写操作挂死或误放行。
    """
    proj = home / "proj"

    async def bad_notify(pending) -> None:
        raise RuntimeError("通道断了")

    gate = ChannelGate(allowed=[], approve_enabled=True, approve_timeout=10,
                       notify=bad_notify, working_dir=proj)
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "broken.txt", "content": "x"})],
        [TextBlock(text="通道坏了也说句话")],
    ])
    agent = make_agent(provider, proj, gate=gate)
    events = await run_eval_turn(agent, "写个文件")  # 不传 decide = deny

    assert resolved(events)[0].decision == "deny", "推卡失败退化为拒绝"
    assert not gate.waiting, "失败的待决项已摘除"
    assert not (proj / "broken.txt").exists()
    done = finished(events)
    assert done["w1"].is_error and "denied" in done["w1"].preview.lower()
    assert events[-1].kind == "turn_finished" and events[-1].stop_reason == "end_turn"
