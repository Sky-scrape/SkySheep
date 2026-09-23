"""H2 回归：压缩与落库/重载的口径。

两个缺陷都出在「内存历史 vs 存储历史」的接缝上：

1. 轮末落库按 `history[n_before:]` 切片，而 compact_history 会在本轮内把
   history 整体换成更短的新列表——旧下标随即失效：新长度 ≤ n_before 时切片
   为空（本轮用户消息与回答全部不落库，重启即丢），略大时又会把早已入库的
   旧消息重复插入。改为按消息 id 身份挑「本轮新增」。
2. system 消息从不落库（它由 compose_system() 实时生成），而重载走
   `load_history(msgs or [Message.system(...)])`——只兜住了空会话，非空会话
   重载后 history[0] 变成 user 消息，下一轮就带着「没有系统提示词」的历史
   去调模型。统一走 _reload_agent_history()：载入后补回 system。
"""

from __future__ import annotations

from conftest import FakeProvider

from skysheep.messages import TextBlock
from skysheep.server.backend import ServerBackend

SUMMARY_MARK = "earlier-conversation-summary"


async def _mk(home, prov):
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: prov)
    await be.setup()
    return be


async def _noop(ev):
    pass


async def test_compaction_turn_still_persists(home):
    """第 3 轮触发压缩后，内存历史比轮前更短——本轮消息仍必须完整落库。"""
    # 调用序：轮1 回答 / 轮2 摘要+回答 / 轮3 摘要+回答
    prov = FakeProvider([
        [TextBlock(text="回答一")],
        [TextBlock(text="摘要一：前情提要。")],
        [TextBlock(text="回答二")],
        [TextBlock(text="摘要二：又压了一次。")],
        [TextBlock(text="回答三")],
    ])
    be = await _mk(home, prov)
    # cfg.context_limit_tokens 有 ge=4000 下限，这里直接覆盖取值方法把上限压到极小，
    # 让压缩在「历史条数刚够」时就触发（keep_recent=2 → 第 2 轮起可能压缩）。
    be._context_limit = lambda: 50
    be.cfg.compaction_keep_recent = 2

    try:
        for i in range(2):
            await be.send(f"第{i + 1}轮提问", _noop)
        sid = be.session.id
        # 第 3 轮的轮前历史长度：旧实现就是拿它当切片起点（n_before）
        n_before = len(be.runtimes[sid].agent.history)
        await be.send("第3轮提问", _noop)

        hist = be.runtimes[sid].agent.history
        # 前置条件：本轮确实触发了压缩，且压缩后历史不长于轮前——
        # 这正是旧下标切片会算出空集、整轮不落库的条件
        assert any(SUMMARY_MARK in m.text for m in hist), "本用例需要触发压缩"
        assert len(hist) <= n_before, "压缩后历史应变短，否则本用例证明不了任何事"
        assert len(hist) < n_before + 2, "本轮新增的两条消息应在压缩后的历史里"

        db = await be.store.load_messages(sid)
        # 三轮问答一条不少、一条不重
        assert [m.role for m in db] == ["user", "assistant"] * 3
        assert [m.text for m in db if m.role == "user"] == [
            "第1轮提问", "第2轮提问", "第3轮提问",
        ]
        assert [m.text for m in db if m.role == "assistant"] == [
            "回答一", "回答二", "回答三",
        ]
        # 压缩摘要不入库：它是引擎产物，入库会被前端渲染成一条假的用户气泡，
        # 还会进 FTS 索引与会话导出
        assert not any(SUMMARY_MARK in m.text for m in db)
    finally:
        await be.shutdown()


async def test_reload_keeps_system_prompt(home):
    """重载会话后 system 提示词必须还在最前面，且下一轮真的发给了模型。"""
    prov = FakeProvider([
        [TextBlock(text="回答一")],
        [TextBlock(text="回答二")],
    ])
    be = await _mk(home, prov)
    try:
        await be.send("你好", _noop)
        sid = be.session.id
        assert be.runtimes[sid].agent.history[0].role == "system"

        # 模拟重启/切会话：从存储重载
        await be.resume_session(sid)
        hist = be.runtimes[sid].agent.history
        assert hist[0].role == "system" and hist[0].text.strip(), "重载后丢了系统提示词"
        assert [m.role for m in hist] == ["system", "user", "assistant"]

        await be.send("再来一轮", _noop)
        sent = prov.calls[-1]
        assert sent[0].role == "system", "重载后的下一轮必须带着系统提示词去调模型"
        assert [m.role for m in sent] == ["system", "user", "assistant", "user"]
    finally:
        await be.shutdown()


async def test_empty_session_reload_has_system(home):
    """空会话重载（旧写法唯一兜住的分支）行为不回退。"""
    be = await _mk(home, FakeProvider([[TextBlock(text="回答一")]]))
    try:
        sid = (await be.new_session())["id"]
        await be.resume_session(sid)
        hist = be.runtimes[sid].agent.history
        assert [m.role for m in hist] == ["system"]
    finally:
        await be.shutdown()
