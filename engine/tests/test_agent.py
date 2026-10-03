"""Agent 核心循环测试（FakeProvider 脚本驱动）。"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import FakeProvider, SlowTextProvider
from pydantic import BaseModel

from skysheep.core import Agent
from skysheep.messages import ImageBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock
from skysheep.models.anthropic_provider import to_anthropic_messages
from skysheep.models.openai_compat import to_openai_messages
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools
from skysheep.tools.base import Safety, Tool, ToolContext


def make_agent(provider, tmp_path, store=None, project_id=None, max_iterations=10):
    gate = PermissionGate(store=store, project_id=project_id)
    registry = ToolRegistry(default_tools())
    return Agent(
        provider=provider,
        registry=registry,
        gate=gate,
        working_dir=tmp_path,
        max_iterations=max_iterations,
    )


async def collect(agent, text, auto_respond="allow_once"):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request" and auto_respond:
            agent.respond_permission(ev.request_id, auto_respond)
    return events


async def test_plain_text_turn(tmp_path):
    provider = FakeProvider([[TextBlock(text="你好，世界")]])
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "hi")

    kinds = [e.kind for e in events]
    assert "text_delta" in kinds
    assert kinds[-1] == "turn_finished"
    assert events[-1].stop_reason == "end_turn"
    assert events[-1].iterations == 1
    # 历史：user + assistant
    assert [m.role for m in agent.history] == ["user", "assistant"]
    assert agent.history[1].text == "你好，世界"
    # token 统计
    assert agent.total_in_tokens == 11


async def test_tool_call_flow_writes_file(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "a.txt", "content": "hi"})],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "create a.txt with hi")

    # 文件真实写入
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi"
    kinds = [e.kind for e in events]
    assert "permission_request" in kinds          # write 需要确认
    assert "permission_resolved" in kinds
    assert "tool_call_started" in kinds
    finished = [e for e in events if e.kind == "tool_call_finished"]
    assert finished and not finished[0].is_error
    assert "a.txt" in finished[0].preview
    # 两轮迭代后正常结束
    assert events[-1].stop_reason == "end_turn"
    assert events[-1].iterations == 2
    # 历史：user, assistant(tool_use), tool, assistant(text)
    roles = [m.role for m in agent.history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # 工具结果回到了 provider 的第二次调用里
    second_call = provider.calls[1]
    assert any(m.role == "tool" for m in second_call)


async def test_denied_tool_returns_error_result(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "b.txt", "content": "x"})],
            [TextBlock(text="ok, skipped")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "create b.txt", auto_respond="deny")

    assert not (tmp_path / "b.txt").exists()
    tool_msg = [m for m in agent.history if m.role == "tool"][0]
    block = tool_msg.content[0]
    assert block.is_error
    assert "denied" in block.content.lower()
    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert finished.is_error


async def test_readonly_tool_needs_no_permission(tmp_path):
    (tmp_path / "c.txt").write_text("findme", encoding="utf-8")
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="grep", input={"pattern": "findme"})],
            [TextBlock(text="found in c.txt")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "search findme", auto_respond=None)

    kinds = [e.kind for e in events]
    assert "permission_request" not in kinds  # 只读自动放行
    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert "c.txt:1" in finished.preview


async def test_allow_always_whitelists_subsequent_calls(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "x1.txt", "content": "1"})],
            [ToolUseBlock(id="t2", name="write_file", input={"path": "x2.txt", "content": "2"})],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    reqs = []
    async for ev in agent.run_turn("write two files"):
        if ev.kind == "permission_request":
            reqs.append(ev.request_id)
            agent.respond_permission(ev.request_id, "allow_always")

    assert (tmp_path / "x1.txt").exists() and (tmp_path / "x2.txt").exists()
    assert len(reqs) == 1  # 第二次写文件被白名单放行，不再询问


async def test_permission_request_previews_always_rule(tmp_path):
    """确认弹窗上要能看到「总是允许」将写入的规则范围（write_file → 整个工具）。"""
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "x.txt", "content": "1"})],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    previews = []
    async for ev in agent.run_turn("write"):
        if ev.kind == "permission_request":
            previews.append((ev.rule_kind, ev.rule_pattern))
            agent.respond_permission(ev.request_id, "allow_always")
    assert previews == [("always", "")]
    assert (tmp_path / "x.txt").exists()


async def test_unknown_tool_reported_as_error(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="nonexistent_tool", input={})],
            [TextBlock(text="ok")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    await collect(agent, "use magic", auto_respond=None)
    tool_msg = [m for m in agent.history if m.role == "tool"][0]
    assert tool_msg.content[0].is_error
    assert "unknown tool" in tool_msg.content[0].content


async def test_max_iterations_stops(tmp_path):
    tu = [ToolUseBlock(id="loop", name="list_dir", input={})]
    provider = FakeProvider([tu]).with_default(tu)
    agent = make_agent(provider, tmp_path, max_iterations=3)
    events = await collect(agent, "loop", auto_respond=None)

    assert events[-1].kind == "turn_finished"
    assert events[-1].stop_reason == "max_iterations"
    assert events[-1].iterations == 3


# ---- M12：取消路径的三处收尾 ----


async def test_pending_permission_cleared_on_cancel(tmp_path):
    """取消后未决权限必须清掉：残留 request_id 不能再被「成功投递」。"""
    import asyncio

    provider = FakeProvider([[
        ToolUseBlock(id="t1", name="write_file", input={"path": "a.txt", "content": "x"}),
    ]])
    agent = make_agent(provider, tmp_path)
    seen: dict = {}

    agen = agent.run_turn("写个文件")

    async def consume():
        async for ev in agen:
            if ev.kind == "permission_request":
                seen["rid"] = ev.request_id
                await asyncio.sleep(30)  # 模拟用户没点确认，轮次被取消

    task = asyncio.create_task(consume())
    for _ in range(200):
        if "rid" in seen:
            break
        await asyncio.sleep(0.01)
    assert "rid" in seen, "没等到权限请求"
    assert agent._pending, "弹窗期间应有未决请求"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 消费方收尾：关掉这一轮的生成器（后端在取消后也会显式 clear_pending，
    # 这里验证 Agent 侧的兜底——关掉生成器就要清未决权限）
    await agen.aclose()
    assert agent._pending == {}, "轮次结束后不得残留未决权限"
    assert agent.respond_permission(seen["rid"], "allow_once") is False


async def test_cancelled_tool_loop_is_repaired_before_next_turn(tmp_path):
    """断裂历史（有 tool_use 无 tool_result）在下一轮开始前自动补上。"""
    provider = FakeProvider([[TextBlock(text="接着做")]])
    agent = make_agent(provider, tmp_path)
    # 手工构造「上一轮被取消」的断口：assistant 发了 tool_use，结果没落进历史
    agent.load_history([
        Message.user("写个文件"),
        Message.assistant([ToolUseBlock(id="dangling", name="write_file",
                                        input={"path": "a.txt", "content": "x"})]),
    ])
    await collect(agent, "继续", auto_respond=None)

    roles = [m.role for m in agent.history]
    # user / assistant(tool_use) / tool(补的中断结果) / user / assistant
    assert roles == ["user", "assistant", "tool", "user", "assistant"], roles
    repaired = agent.history[2]
    assert repaired.content[0].tool_use_id == "dangling"
    assert repaired.content[0].is_error


async def test_repair_is_noop_when_history_is_consistent(tmp_path):
    """历史已经自洽时不动它（不重复补、不重排）。"""
    provider = FakeProvider([[TextBlock(text="ok")]])
    agent = make_agent(provider, tmp_path)
    await collect(agent, "你好", auto_respond=None)
    before = list(agent.history)
    assert agent.repair_dangling_tool_uses() == []
    assert agent.history == before


# ---- 流式中途取消：已见部分正文要落库（发现 2）----


async def test_cancel_mid_stream_keeps_partial_text(tmp_path):
    """任务级取消（CancelledError 从生成器内部穿透）：已见前缀落库并带停止标注。

    旧缺陷：正文合并在流式循环之后（core/agent.py 的「助手消息并入历史」），
    取消发生在循环中途时 text_parts 随生成器一起丢弃——前端已渲染 400 字，
    落库却一条 assistant 都没有，重启后这轮无声消失。
    """
    provider = SlowTextProvider("内容段落" * 2000)
    agent = make_agent(provider, tmp_path)
    got: list[str] = []

    async def consume():
        async for ev in agent.run_turn("写一篇很长的文章"):
            if ev.kind == "text_delta":
                got.append(ev.text)
                if sum(map(len, got)) >= 400:
                    asyncio.current_task().cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(consume())

    seen = "".join(got)
    assert len(seen) >= 400, "用例需要已收到部分正文"
    # 历史：user + 部分 assistant（正文与已见前缀一致，尾部带停止标注）
    assert [m.role for m in agent.history] == ["user", "assistant"]
    partial = agent.history[-1].text
    assert partial.startswith(seen), "落库正文应是已见前缀（不缺口、不重复）"
    assert partial.endswith("…（已停止）")
    # 历史自洽：下一轮照常能跑，模型上下文里能看到上一轮被截断的正文
    agent.provider = FakeProvider([[TextBlock(text="好的，接着来。")]])
    await collect(agent, "继续", auto_respond=None)
    assert agent.history[-1].text == "好的，接着来。"
    assert any(
        m.role == "assistant" and "…（已停止）" in m.text
        for m in agent.provider.calls[0]
    )


async def test_generator_close_mid_stream_keeps_partial_text(tmp_path):
    """取消的另一种形态：消费方中途关闭生成器（GeneratorExit 路径，finally 里
    不允许 await）——部分正文同样要兜底落库。"""
    provider = SlowTextProvider("内容段落" * 2000)
    agent = make_agent(provider, tmp_path)
    got: list[str] = []
    agen = agent.run_turn("写一篇很长的文章")
    async for ev in agen:
        if ev.kind == "text_delta":
            got.append(ev.text)
            if sum(map(len, got)) >= 400:
                break
    await agen.aclose()

    seen = "".join(got)
    assert len(seen) >= 400, "用例需要已收到部分正文"
    assert [m.role for m in agent.history] == ["user", "assistant"]
    partial = agent.history[-1].text
    assert partial.startswith(seen)
    assert partial.endswith("…（已停止）")


async def test_cancel_before_any_text_leaves_no_empty_assistant(tmp_path):
    """一个正文都没吐就停止：不得落一条空的/纯标注的 assistant 消息。"""
    provider = SlowTextProvider("内容段落" * 2000)
    agent = make_agent(provider, tmp_path)
    agen = agent.run_turn("写一篇很长的文章")
    first = await agen.__anext__()  # 消费到轮首事件即停（正文增量还没开始）
    assert first.kind == "turn_started"
    await agen.aclose()
    assert [m.role for m in agent.history] == ["user"]


async def test_max_iterations_emits_notice(tmp_path):
    """到迭代上限时给明确事件，不再静默断头。"""
    tu = [ToolUseBlock(id="loop", name="list_dir", input={})]
    provider = FakeProvider([tu]).with_default(tu)
    agent = make_agent(provider, tmp_path, max_iterations=2)
    events = await collect(agent, "loop", auto_respond=None)

    notices = [e for e in events if e.kind == "notice"]
    assert notices and "上限" in notices[-1].message
    assert events[-1].kind == "turn_finished"
    assert events[-1].stop_reason == "max_iterations"


async def test_history_includes_tool_args_for_provider(tmp_path):
    payload = {"path": "j.json", "content": json.dumps({"a": 1})}
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input=payload)],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    await collect(agent, "write json", auto_respond="allow_once")

    second_call_messages = provider.calls[1]
    assistant_msg = [m for m in second_call_messages if m.role == "assistant"][0]
    assert assistant_msg.tool_uses[0].input == {"path": "j.json", "content": '{"a": 1}'}
    tool_msg = [m for m in second_call_messages if m.role == "tool"][0]
    assert not tool_msg.content[0].is_error


async def test_todo_write_emits_event(tmp_path):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="td", name="todo_write", input={"todos": [
                {"content": "步骤1", "status": "completed"},
                {"content": "步骤2", "status": "in_progress"},
            ]})],
            [TextBlock(text="ok")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "跑任务", auto_respond=None)

    todo_events = [e for e in events if e.kind == "todo_updated"]
    assert todo_events, "应产出 todo_updated 事件"
    items = todo_events[0].items
    assert [i["content"] for i in items] == ["步骤1", "步骤2"]
    assert items[0]["status"] == "completed"


async def test_write_file_carries_diff(tmp_path):
    (tmp_path / "d.txt").write_text("old line\n", encoding="utf-8")
    provider = FakeProvider(
        [
            [ToolUseBlock(id="w1", name="write_file", input={
                "path": "d.txt", "content": "new line\nanother\n",
            })],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "覆盖文件", auto_respond="allow_once")

    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert "-old line" in finished.diff
    assert "+new line" in finished.diff
    assert "a/d.txt" in finished.diff


async def test_edit_file_carries_diff(tmp_path):
    (tmp_path / "e.py").write_text("def main():\n    pass\n", encoding="utf-8")
    provider = FakeProvider(
        [
            [ToolUseBlock(id="e1", name="edit_file", input={
                "path": "e.py", "old_string": "pass", "new_string": "print('hi')",
            })],
            [TextBlock(text="done")],
        ]
    )
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "编辑文件", auto_respond="allow_once")

    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert "-    pass" in finished.diff
    assert "+    print('hi')" in finished.diff


async def test_cache_hit_tokens_accumulate(tmp_path):
    """Provider 上报缓存命中时按会话累计；分母是含缓存部分的完整输入 token。"""
    from skysheep.models.base import ProviderDone, ProviderTextDelta

    class CachedProvider(FakeProvider):
        # 模拟「提示词缓存生效」的上游：输入 200、其中 180 命中缓存
        async def stream(self, messages, tool_schemas, effort=None):
            blocks = self.scripted.pop(0) if self.scripted else self.scripted_default
            for b in blocks:
                if isinstance(b, TextBlock):
                    yield ProviderTextDelta(b.text)
            yield ProviderDone(
                stop_reason="end_turn", input_tokens=200, output_tokens=5, cached_tokens=180,
            )

    provider = CachedProvider([[TextBlock(text="一")], [TextBlock(text="二")]])
    agent = make_agent(provider, tmp_path)
    await collect(agent, "a")
    await collect(agent, "b")

    assert agent.total_in_tokens == 400
    assert agent.total_cached_tokens == 360


async def test_provider_without_cache_reports_zero(tmp_path):
    """不上报缓存的服务（如 FakeProvider）不产生命中计数，界面据此显示「—」。"""
    provider = FakeProvider([[TextBlock(text="好")]])
    agent = make_agent(provider, tmp_path)
    await collect(agent, "a")

    assert agent.total_in_tokens == 11
    assert agent.total_cached_tokens == 0


# ---- 工具产生的图片按调用归属（回归：并发只读批不再整桶收割共享 ctx.images） ----


class _EmptyArgs(BaseModel):
    pass


class _ImageTool(Tool):
    """往 ctx.images 追加一张图的只读工具（复刻 screenshot 的产出形态）。"""

    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = _EmptyArgs

    def __init__(self, tag: str) -> None:
        super().__init__()
        self.name = tag
        self.description = f"fake screenshot {tag}"

    async def run(self, args, ctx: ToolContext) -> str:
        await asyncio.sleep(0.01)  # 让同批多个调用真并发
        ctx.images.append(ImageBlock(media_type="image/png", data=self.name))
        return f"{self.name} ok"


async def test_concurrent_readonly_batch_attributes_images_per_call(tmp_path):
    """同一批并发只读调用的截图各自带各自的图（发现 34 回归）。

    旧实现收结果时在第一条非错误结果处 `attached = list(ctx.images);
    ctx.images.clear()`，同批第二张截图被错配到第一个工具名下。
    """
    provider = FakeProvider([
        [
            ToolUseBlock(id="t1", name="shot_a", input={}),
            ToolUseBlock(id="t2", name="shot_b", input={}),
        ],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, tmp_path)
    agent.registry.register(_ImageTool("shot_a"))
    agent.registry.register(_ImageTool("shot_b"))
    events = await collect(agent, "拍两张")

    finished = [e for e in events if e.kind == "tool_call_finished"]
    assert [e.name for e in finished] == ["shot_a", "shot_b"]
    assert [[b["data"] for b in e.images] for e in finished] == [["shot_a"], ["shot_b"]]
    # 历史：两条图片 user 消息，各带各的图（模型侧全部可见，不重复不丢失）
    img_user_msgs = [
        m for m in agent.history
        if m.role == "user" and any(isinstance(b, ImageBlock) for b in m.content)
    ]
    assert [
        [b.data for b in m.content if isinstance(b, ImageBlock)] for m in img_user_msgs
    ] == [["shot_a"], ["shot_b"]]


async def test_serial_image_tool_still_attaches(tmp_path):
    """串行路径对照：单个只读产图工具的图仍附在它自己的结果上。"""
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="shot_a", input={})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, tmp_path)
    agent.registry.register(_ImageTool("shot_a"))
    events = await collect(agent, "拍一张")

    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert not finished.is_error
    assert [b["data"] for b in finished.images] == ["shot_a"]


# ---- 并发批产图的历史序列化（发现 7 回归：截图 user 消息不得穿插在 tool_result 之间） ----


def _anthropic_orphans(wire: list[dict]) -> list[str]:
    """anthropic 线格式里的孤儿 tool_result：user(tool_result) 前面不是携带
    配对 tool_use 的 assistant（违反即 API 400）。"""
    bad: list[str] = []
    for idx, m in enumerate(wire):
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            tid = b.get("tool_use_id") if isinstance(b, dict) else None
            if not tid or b.get("type") != "tool_result":
                continue
            prev = wire[idx - 1] if idx else None
            paired = bool(prev) and prev["role"] == "assistant" and any(
                blk.get("type") == "tool_use" and blk.get("id") == tid
                for blk in prev["content"]
            )
            if not paired:
                bad.append(tid)
    return bad


def _openai_orphans(wire: list[dict]) -> list[str]:
    """openai 线格式里的孤儿 tool 消息：向前越过连续 tool 消息后必须紧跟
    携带配对 tool_calls 的 assistant（违反即 API 400）。"""
    bad: list[str] = []
    for idx, m in enumerate(wire):
        if m["role"] != "tool":
            continue
        k = idx - 1
        while k >= 0 and wire[k]["role"] == "tool":
            k -= 1
        calls: list[dict] = []
        if k >= 0 and wire[k]["role"] == "assistant":
            calls = wire[k].get("tool_calls") or []
        if m["tool_call_id"] not in [c["id"] for c in calls]:
            bad.append(m["tool_call_id"])
    return bad


def _assert_wire_pairs(history: list[Message]) -> None:
    """两侧序列化都不得有孤儿 tool_result（发现 7 验收断言）。"""
    assert _anthropic_orphans(to_anthropic_messages(history)) == []
    assert _openai_orphans(to_openai_messages(history)) == []
    # 反向：每个 tool_use 的 tool_result 也在（无断头 tool_use）
    tu_ids = {tu.id for m in history for tu in m.tool_uses}
    tr_ids = {
        b.tool_use_id for m in history for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    assert tu_ids == tr_ids


async def test_concurrent_batch_screenshots_after_all_tool_results(tmp_path):
    """并发批产图（发现 7 回归）：截图 user 消息统一落在整批 tool_result 之后，
    不得穿插在工具结果之间——穿插会把 tool_result 与配对的 assistant(tool_use)
    隔开，anthropic 与 openai 两侧序列化都出孤儿 tool_result，当轮下一次模型
    调用两家 API 必 400（协议死局）。"""
    (tmp_path / "c.txt").write_text("findme", encoding="utf-8")
    provider = FakeProvider([
        [
            ToolUseBlock(id="c1", name="shot_a", input={}),
            ToolUseBlock(id="c2", name="shot_b", input={}),
            ToolUseBlock(id="c3", name="grep", input={"pattern": "findme"}),
        ],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, tmp_path)
    agent.registry.register(_ImageTool("shot_a"))
    agent.registry.register(_ImageTool("shot_b"))
    events = await collect(agent, "拍两张再搜一下", auto_respond=None)

    # 历史：assistant 的全部 tool_result 先落齐，截图 user 消息统一在其后
    roles = [m.role for m in agent.history]
    assert roles == [
        "user", "assistant", "tool", "tool", "tool", "user", "user", "assistant",
    ]
    img_user_idx = [
        k for k, m in enumerate(agent.history)
        if m.role == "user" and any(isinstance(b, ImageBlock) for b in m.content)
    ]
    tool_result_idx = [k for k, m in enumerate(agent.history) if m.role == "tool"]
    assert img_user_idx and min(img_user_idx) > max(tool_result_idx)
    # 截图归属不乱：每条截图 user 消息仍各带各的图
    assert [
        [b.data for b in agent.history[k].content if isinstance(b, ImageBlock)]
        for k in img_user_idx
    ] == [["shot_a"], ["shot_b"]]

    # 事件线不受影响：ToolCallFinished 仍按调用即时带各自的图（与历史两条线）
    finished = [e for e in events if e.kind == "tool_call_finished"]
    assert [e.name for e in finished] == ["shot_a", "shot_b", "grep"]
    assert [[b["data"] for b in e.images] for e in finished[:2]] == [["shot_a"], ["shot_b"]]
    assert finished[2].images == []

    _assert_wire_pairs(agent.history)


async def test_mixed_serial_batch_screenshot_after_all_tool_results(tmp_path):
    """产图只读工具与需确认工具同一条 assistant 消息（串行混批，发现 7 同根）：
    截图 user 消息同样等全部 tool_result 落齐后才追加，序列化不留孤儿。"""
    provider = FakeProvider([
        [
            ToolUseBlock(id="c1", name="shot_a", input={}),
            ToolUseBlock(id="c2", name="write_file", input={"path": "d.txt", "content": "x"}),
        ],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, tmp_path)
    agent.registry.register(_ImageTool("shot_a"))
    events = await collect(agent, "拍一张再写文件")

    assert (tmp_path / "d.txt").exists()
    roles = [m.role for m in agent.history]
    assert roles == ["user", "assistant", "tool", "tool", "user", "assistant"]
    # 事件线：shot_a 的完成事件仍带自己的图
    finished = [e for e in events if e.kind == "tool_call_finished"]
    assert [e.name for e in finished] == ["shot_a", "write_file"]
    assert [b["data"] for b in finished[0].images] == ["shot_a"]
    assert finished[1].images == []

    _assert_wire_pairs(agent.history)


# ---- 并发只读批的收集期门预检（安全审查回归：只读批绕门） ----
# memory_write 名义 READONLY 却写 ~/.skysheep/memory.md（注入所有会话的
# system prompt），权限门对它有先于 READONLY 短路的逐次确认守卫。旧实现
# 批收集只看 safety 分级：模型一轮先发真只读工具（read_file）再紧跟
# memory_write，后者被收进并发批零确认直达执行，守卫被整段架空。


def _batch_with_memory_write_provider() -> FakeProvider:
    return FakeProvider([
        [
            ToolUseBlock(id="t1", name="read_file", input={"path": "c.txt"}),
            ToolUseBlock(
                id="t2", name="memory_write",
                input={"action": "append", "content": "INJECTED-BY-BATCH"},
            ),
        ],
        [TextBlock(text="done")],
    ])


async def test_batch_member_memory_write_breaks_batch_and_asks(home):
    """真只读工具后紧跟 memory_write：后者必须断批走串行确认，不得零确认执行。"""
    (home / "proj" / "c.txt").write_text("hello", encoding="utf-8")
    agent = make_agent(_batch_with_memory_write_provider(), home / "proj")
    events = await collect(agent, "读完顺便记住一件事", auto_respond="deny")

    kinds = [e.kind for e in events]
    # memory_write 不能被收进并发批：必须弹确认（旧实现 permission_request 数为 0）
    assert kinds.count("permission_request") == 1
    req = [e for e in events if e.kind == "permission_request"][0]
    assert req.tool_name == "memory_write"
    # 拒绝后全局记忆不落盘（SKYSHEEP_HOME 已由 home 夹具隔离）
    assert not (home / "home" / "memory.md").exists()
    finished = {e.tool_call_id: e for e in events if e.kind == "tool_call_finished"}
    assert finished["t2"].is_error
    assert "denied" in finished["t2"].preview.lower()
    # 同批的真只读工具不受影响：照常执行、无错误
    assert not finished["t1"].is_error
    assert (home / "proj" / "c.txt").read_text(encoding="utf-8") == "hello"
    # 轮次正常收尾
    assert events[-1].kind == "turn_finished"


async def test_batch_member_memory_write_confirm_then_allow(home):
    """确认后允许：串行路径照常写入全局记忆（修复只恢复门，不砍功能）。"""
    agent = make_agent(_batch_with_memory_write_provider(), home / "proj")
    events = await collect(agent, "读完顺便记住一件事", auto_respond="allow_once")

    memory_file = home / "home" / "memory.md"
    assert memory_file.exists()
    assert "INJECTED-BY-BATCH" in memory_file.read_text(encoding="utf-8")
    finished = {e.tool_call_id: e for e in events if e.kind == "tool_call_finished"}
    assert not finished["t2"].is_error
    assert events[-1].kind == "turn_finished"


async def test_pure_readonly_batch_still_concurrent(home):
    """守卫不扩大打击面：全真只读的批仍免确认并发（事件顺序不乱、无确认）。"""
    (home / "proj" / "c.txt").write_text("findme", encoding="utf-8")
    provider = FakeProvider([
        [
            ToolUseBlock(id="t1", name="read_file", input={"path": "c.txt"}),
            ToolUseBlock(id="t2", name="grep", input={"pattern": "findme"}),
        ],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj")
    events = await collect(agent, "都看看", auto_respond=None)

    kinds = [e.kind for e in events]
    assert "permission_request" not in kinds
    finished = [e for e in events if e.kind == "tool_call_finished"]
    assert [e.name for e in finished] == ["read_file", "grep"]
    assert all(not e.is_error for e in finished)
    assert events[-1].kind == "turn_finished"
