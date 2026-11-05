"""协作行为场景回归（fake provider 驱动真实后端协作管道）。

三个种子场景，都走真实的 ServerBackend 协作路径（send 的 roundtable / team /
adversarial 三分支 → _roundtable_body / _team_body / _adversarial_body → 真实
编排器与角色流水线），出处见各用例 docstring：

- 圆桌融合：主席也出草稿（chair_answers 默认开）+ 两名成员独立作答 + 主席
  融合——成员草稿不进主历史、融合答案带 roundtable 元数据落库，融合调用确实
  吃到了全部草稿；
- 团队闭环：AI 总管自动闭环（拆解 → 派工 → 验收 → 交付）——《交付说明》随
  team_finished 落库，user+assistant 成对可审计，工单板走完状态机；
- 对抗裁决：发现 → 调查 → 建议 → 裁判四阶段——成立/部分成立/推翻的裁决
  统计随 adversarial_finished 下发，终审报告带对抗元数据落库，用量按角色
  入账。

角色脚本全部 FakeProvider 池化回放（按装配顺序弹池：主模型在 setup 时先取一颗，
之后每个成员/角色构建各取一颗；圆桌的主席=主模型，先出草稿再融合，两次调用都
从同一颗脚本按序取）；不联网、不碰真实 home，被审内容与命令一律无副作用。

本文件是场景本体的唯一事实源：evals/test_eval_collab_scenarios.py 把同一批
场景函数挂进 evals 评测基线（eval 标记由 evals/conftest 自动打，`-m eval` 跑），
两处跑的是同一份实现，不存在两套断言漂移。
"""

from __future__ import annotations

import json

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend


async def _make_backend(home, pool) -> ServerBackend:
    """组装真实后端（SKYSHEEP_HOME 已隔离）。pool 按装配顺序弹池：主模型在
    setup 时先取一颗，之后每个成员/角色构建各取一颗。预置 ui 偏好：关掉更新
    检查（评测不联网）与系统通知（不打扰真实桌面）。"""
    prefs_dir = home / "home"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui.json").write_text('{"update_check": 0, "notify": 0}', encoding="utf-8")
    be = ServerBackend(
        working_dir=home / "proj",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="脚本耗尽的兜底回答")]]),
    )
    await be.setup()
    return be


def _collector():
    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    return events, emit


def _kinds(events) -> list[str]:
    return [e.get("kind") for e in events]


def _msg_text(msg: dict) -> str:
    """assistant_message 事件负载里的正文：Message.text 是属性不进
    model_dump，事件负载按 content 块取文本。"""
    return "".join(
        str(b.get("text") or "") for b in (msg.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text"
    )


# ---- 场景 31：圆桌融合 ----

async def test_collab_roundtable_fusion(home):
    """圆桌融合行为基线：主席也出草稿（chair_answers 默认开）+ 两名成员独立
    作答 + 主席融合。断言的都是组装行为而不是文本质量：
    - 事件序列：roundtable_started（3 个草稿位，主席在前）→ 每位成员
      roundtable_member_finished（done）→ assistant_message（融合答案）→
      turn_finished；
    - 成员草稿不进主历史：库里只有 user 提问 + assistant 融合答案；
    - 融合答案带 roundtable 元数据落库（刷新/回放卡展开回看的事实源）；
    - 融合调用确实吃到了全部草稿：主席第二次调用的输入里能找到两份成员草稿
      与主席自己的草稿（融合提示词的输入契约）。"""
    chair = FakeProvider([
        [TextBlock(text="主席草稿：先给一版结构化骨架。")],
        [TextBlock(text="融合后的最终答案：按骨架补全细节。")],
    ])
    m1 = FakeProvider([[TextBlock(text="成员一草稿：补充数据面论据。")]])
    m2 = FakeProvider([[TextBlock(text="成员二草稿：补充风险与替代方案。")]])
    be = await _make_backend(home, [chair, m1, m2])
    try:
        await be.new_session()
        events, emit = _collector()
        result = await be.send(
            "评审这个设计并给出最终建议", emit,
            roundtable=True,
            members=[{"provider": "m1", "model": "m"},
                     {"provider": "m2", "model": "m"}],
        )
        assert result.get("stopped") is not True, result

        started = next(e for e in events if e.get("kind") == "roundtable_started")
        assert len(started["members"]) == 3, "主席草稿在前 + 两名成员"
        assert all("provider" in m and "model" in m for m in started["members"])

        finished = [e for e in events
                    if e.get("kind") == "roundtable_member_finished"]
        assert len(finished) == 3 and all(e["status"] == "done" for e in finished)

        fused = [e for e in events if e.get("kind") == "assistant_message"]
        assert len(fused) == 1
        msg = fused[0]["message"]
        assert "融合后的最终答案" in _msg_text(msg)
        meta = msg.get("roundtable") or {}
        drafts = " ".join(str((m or {}).get("draft") or "")
                          for m in (meta.get("members") or []))
        assert "成员一草稿" in drafts and "成员二草稿" in drafts, meta.get("members")

        # 成员草稿不进主历史：库里只有 user 提问 + assistant 融合答案
        msgs = await be.store.load_messages(be.session.id)
        roles = [m.role for m in msgs if m.role in ("user", "assistant")]
        assert roles == ["user", "assistant"], roles
        assert "融合后的最终答案" in msgs[-1].text

        # 融合调用的输入契约：主席第二次调用能看到全部草稿（含主席自己的）
        fusion_in = json.dumps([m.model_dump() for m in chair.calls[1]],
                               ensure_ascii=False)
        assert "成员一草稿" in fusion_in and "成员二草稿" in fusion_in
        assert "主席草稿" in fusion_in
    finally:
        await be.shutdown()


# ---- 场景 32：团队闭环 ----

# 总管脚本按「轮内续跑」语义排布：team_assign 的工具结果会让总管在同一轮
# 内续跑一次（脚本第 2 项无工具=本轮结束，触发派工与队员执行）；队员报告
# 落板（待验收）后总管被再次唤醒，第 3 项验收、第 4 项在同轮续跑里交付——
# 闭环每一步的脚本位都是确定的，不依赖时序。
_DIRECTOR_CLOSED_LOOP = [
    [TextBlock(text="分工方案：调研先行"), ToolUseBlock(
        id="d1", name="team_assign",
        input={"title": "巡检目录", "assignee": "队员0", "type": "exec"})],
    [TextBlock(text="（等待队员执行）")],
    [TextBlock(text="对照验收标准：合格"), ToolUseBlock(
        id="d2", name="team_accept", input={"task_id": "T1"})],
    [TextBlock(text="《交付说明》：巡检完成。"), ToolUseBlock(
        id="d3", name="team_deliver", input={})],
]


async def test_collab_team_closed_loop(home):
    """团队闭环行为基线：AI 总管自动闭环（拆解 → 派工 → 验收 → 交付）。
    断言的都是组装行为而不是文本质量：
    - 事件序列：team_started（名册就位、AI 总管）→ 工单派工与状态机推进
      （team_task_updated 到 done）→ 频道消息（派工与队员报告定稿）→
      team_finished（done，summary 带《交付说明》）→ assistant_message
      （团队纪要带 team 元数据落库）；
    - 会话落库 user+assistant 成对，可审计可回放；
    - 用量按成员入账（成员与总管各一行，归属本次会话）。"""
    member = FakeProvider([[TextBlock(text="队员0执行完成：巡检完成。")]])
    director = FakeProvider(_DIRECTOR_CLOSED_LOOP)
    be = await _make_backend(home, [FakeProvider([[TextBlock(text="主模型不参与")]]),
                                    director, member])
    try:
        await be.new_session()
        events, emit = _collector()
        result = await be.send(
            "巡检目录并汇总", emit,
            team=True, director_mode="ai",
            director={"provider": "dprov", "model": "dm"},
            members=[{"provider": "mprov", "model": "mm", "name": "队员0"}],
        )
        assert result.get("stopped") is not True, result

        started = next(e for e in events if e.get("kind") == "team_started")
        assert started["director_mode"] == "ai"
        assert [m["name"] for m in started["roster"]] == ["队员0"]

        task_events = [e for e in events if e.get("kind") == "team_task_updated"]
        assert task_events, "工单派工与状态机推进必须有事件"
        assert any(t["task_id"] == "T1" and t["status"] == "done"
                   for t in task_events), [t["status"] for t in task_events]

        finished = next(e for e in events if e.get("kind") == "team_finished")
        assert finished["status"] == "done"
        assert "《交付说明》" in finished["summary"]

        digest = [e for e in events if e.get("kind") == "assistant_message"]
        assert digest, "团队纪要必须随 assistant_message 下发"
        tail = digest[-1]["message"]
        team_meta = tail.get("roundtable") or {}
        assert team_meta.get("mode") == "team", "团队元数据随纪要落库（复用槽位）"
        assert (team_meta.get("team") or {}).get("status") == "done"
        assert "《交付说明》" in _msg_text(tail), "纪要附终态交付说明"

        msgs = await be.store.load_messages(be.session.id)
        assert any(m.role == "user" and "巡检目录并汇总" in m.text
                   for m in msgs), "用户指令落库"
        assert any(m.role == "assistant" and "《交付说明》" in m.text
                   for m in msgs), "交付说明成对落库可审计"
        assert await be.store.usage_today() > 0, "成员与总管用量按角色入账"
    finally:
        await be.shutdown()


# ---- 场景 33：对抗裁决 ----

_FINDER_JSON = json.dumps({
    "findings": [
        {"category": "bug", "severity": "high", "location": "a.py:10",
         "description": "空列表调用 pop 会越界", "evidence": "第 10 行没有判空"},
        {"category": "security", "severity": "critical", "location": "b.py:3",
         "description": "拼接 SQL 有注入风险", "evidence": "格式化字符串进查询"},
        {"category": "performance", "severity": "low", "location": "c.py:1",
         "description": "循环里重复编译正则", "evidence": "每次迭代都 re.compile"},
    ]
}, ensure_ascii=False)

_INVESTIGATOR_JSON = json.dumps({
    "verdicts": [
        {"id": "F-001", "verdict": "confirmed", "reason": "确实没有判空"},
        {"id": "F-002", "verdict": "partial",
         "reason": "有参数化查询兜底，但日志里仍拼了原文",
         "corrected_description": "日志记录了原始查询串，可能泄露敏感参数",
         "corrected_severity": "low"},
        {"id": "F-003", "verdict": "refuted",
         "reason": "正则编译结果被外层 lru_cache 复用"},
    ]
}, ensure_ascii=False)

_ADVISOR_JSON = json.dumps({
    "solutions": [
        {"id": "F-001", "fix": "先判空再取元素，或用 next(iter, default)",
         "risk": "低", "priority": "P0"},
        {"id": "F-002", "fix": "日志脱敏后再落盘", "risk": "低", "priority": "P1"},
    ]
}, ensure_ascii=False)

_JUDGE_REPORT = ("## 对抗审查报告\n\n### 1. a.py:10 空列表越界\n\n"
                 "修复：先判空。\n\n总体：一处高危需立即处理。")


async def test_collab_adversarial_verdicts(home):
    """对抗裁决行为基线：发现 → 调查 → 建议 → 裁判四阶段。断言的都是组装
    行为而不是文本质量：
    - 事件序列：adversarial_started（四角色名册）→ 四个阶段步进 → 3 条候选
      问题逐条上卡 → 3 条裁决（confirmed/partial/refuted）→ 裁判报告流式 →
      adversarial_finished（done，统计齐全）；
    - 终审报告以带 adversarial 元数据的 assistant 消息落库（status=done，
      问题清单与修复方案随元数据持久化）；
    - 用量按角色入账（四角色各一行，归属本次会话）。"""
    judge = FakeProvider([[TextBlock(text=_JUDGE_REPORT)]])
    finder = FakeProvider([[TextBlock(text=_FINDER_JSON)]])
    investigator = FakeProvider([[TextBlock(text=_INVESTIGATOR_JSON)]])
    advisor = FakeProvider([[TextBlock(text=_ADVISOR_JSON)]])
    be = await _make_backend(home, [judge, finder, investigator, advisor])
    try:
        await be.new_session()
        events, emit = _collector()
        result = await be.send(
            "审查这段代码：print(pop([]))", emit,
            adversarial=True,
            members=[{"provider": "prov-f", "role": "finder"},
                     {"provider": "prov-i", "role": "investigator"},
                     {"provider": "prov-a", "role": "advisor"}],
        )
        assert result.get("stopped") is not True, result

        kinds = _kinds(events)
        started = next(e for e in events if e.get("kind") == "adversarial_started")
        assert [r["role"] for r in started["roles"]] == [
            "finder", "investigator", "advisor", "judge"]
        assert kinds.count("adversarial_phase") == 4
        assert kinds.count("adversarial_finding_proposed") == 3
        assert kinds.count("adversarial_verdict") == 3, "三条候选全部有裁决"
        assert "text_delta" in kinds, "裁判报告流式"
        fin = next(e for e in events if e.get("kind") == "adversarial_finished")
        assert fin["status"] == "done"
        assert "成立 1" in fin["summary"] and "推翻 1" in fin["summary"]

        reported = [e for e in events if e.get("kind") == "assistant_message"]
        assert reported, "终审报告必须随 assistant_message 下发"
        msg = reported[-1]["message"]
        assert "对抗审查报告" in _msg_text(msg)
        meta = msg.get("adversarial") or {}
        assert meta.get("status") == "done"
        verdicts = [f.get("verdict") for f in (meta.get("findings") or [])]
        assert verdicts == ["confirmed", "partial", "refuted"], verdicts
        assert any(f.get("solution") for f in (meta.get("findings") or []))

        msgs = await be.store.load_messages(be.session.id)
        assert any(m.role == "assistant" and m.adversarial
                   and (m.adversarial or {}).get("status") == "done"
                   for m in msgs), "报告消息带对抗元数据落库"
        assert await be.store.usage_today() > 0, "用量按角色入账"
    finally:
        await be.shutdown()
