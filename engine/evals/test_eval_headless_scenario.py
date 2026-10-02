"""评测基线 · headless 一次性运行（skysheep run）审计输出场景。

场景 24/25：非交互跑一轮，钉住「无人值守也要留下可审计痕迹」的组装行为：
- 结构化结果：stop_reason / provider / 回复正文 / token 用量齐备；
- 会话落库可审计：自动建「▶ 指令前缀」标题的会话，本轮 user / assistant
  （含工具调用）/ tool 消息按序落进 skysheep.db——事后能从库里完整回放
  无人值守跑过什么；
- 无人值守门控（HeadlessGate）：未预授权的写自动拒绝、不落盘、不阻塞，
  Agent 收到拒绝继续收尾，拒绝痕迹同样落库可审计。

走 cli/app.py 的 run_headless 真实入口（与定时任务/流水线共用的引擎装配），
SKYSHEEP_HOME 隔离，会话库落在临时目录。
"""

from __future__ import annotations

from skysheep.cli.app import run_headless
from skysheep.config import db_path
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.session.store import SessionStore


async def test_eval_headless_run_leaves_auditable_session(home):
    """场景 24（headless 审计输出）：结构化结果 + 会话与消息落库可审计。

    用带一次只读工具调用的脚本跑 run_headless（输出 json，不向 stdout 喷流式
    文本）：结果里 stop_reason=end_turn、回复正文、用量齐备；库里能找到标题
    以「▶」开头的会话，消息序列 user → assistant(tool_use) → tool → assistant
    完整落库——无人值守跑过什么、用过什么工具，事后可回放。
    """
    proj = home / "proj"
    (proj / "c.txt").write_text("hello", encoding="utf-8")
    provider = FakeProvider([
        [ToolUseBlock(id="r1", name="list_dir", input={"path": "."})],
        [TextBlock(text="盘点完成：目录里有 c.txt。")],
    ])
    result = await run_headless(
        prompt="盘点当前目录",
        directory=str(proj),
        provider_factory=lambda: provider,
        output="json",  # 评测不向 stdout 喷流式文本
    )
    assert result["stop_reason"] == "end_turn"
    assert result["provider"] == "fake"
    assert result["error"] is None
    assert result["reply"] == "盘点完成：目录里有 c.txt。"
    assert result["usage"]["input"] > 0 and result["usage"]["output"] > 0
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "assistant"], (
        "结构化结果透出本轮 user/assistant 消息（tool 行不透出）"
    )

    # 落库审计：会话与全部消息都在库里（按全库会话查，不依赖项目行解析形态）
    store = await SessionStore(db_path()).connect()
    try:
        sessions = await store.list_sessions()
        assert len(sessions) == 1
        sess = sessions[0]
        assert sess.title.startswith("▶ "), "headless 会话用「▶ 指令前缀」标题"
        assert "盘点当前目录" in sess.title
        msgs = await store.load_messages(sess.id)
        assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[0].text == "盘点当前目录"
        assert msgs[1].tool_uses[0].name == "list_dir", "用过什么工具可审计"
        assert msgs[2].content[0].tool_use_id == "r1"
        assert msgs[3].text == "盘点完成：目录里有 c.txt。"
    finally:
        await store.close()


async def test_eval_headless_run_denied_write_audited_and_not_landed(home):
    """场景 25（headless 门控 + 审计）：未预授权的写被自动拒绝，拒绝可审计。

    HeadlessGate 默认名单为空：write_file 立刻拒绝、不落盘、Agent 继续收尾
    （turn 不挂死）；落库的 assistant 消息带着这次 write_file 尝试——
    「无人值守想写什么」本身就是要能审计的事实。
    """
    proj = home / "proj"
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="没写成，汇报完毕。")],
    ])
    result = await run_headless(
        prompt="写个文件",
        directory=str(proj),
        provider_factory=lambda: provider,
        allow_tools=[],  # 不预授权任何写工具
        output="json",
    )
    assert result["stop_reason"] == "end_turn", "拒绝后 Agent 继续收尾，轮不挂死"
    assert result["reply"] == "没写成，汇报完毕。"
    assert not (proj / "secret.txt").exists(), "未授权写入不得落盘"

    store = await SessionStore(db_path()).connect()
    try:
        sessions = await store.list_sessions()
        assert len(sessions) == 1
        msgs = await store.load_messages(sessions[0].id)
        assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[1].tool_uses[0].name == "write_file", "写入尝试落库可审计"
        assert msgs[2].content[0].is_error, "拒绝以错误结果回给模型"
    finally:
        await store.close()
