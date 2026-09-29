"""CLI REPL 打断路径（评审发现 10）：

- 轮中 Ctrl-C 在 Windows + Python 3.11+ 的 asyncio.run 下实际以 CancelledError
  投递（KeyboardInterrupt 只在 Runner 外层抛）：打断分支两条异常都要接住，
  否则异常穿透出去整个 REPL 直接退出、连「已打断」提示都不出现。
- 打断的一轮也要落库：用户消息在 run_turn 开头就进了内存 history，不落库的话
  /resume 与重启从库重载后这一轮即丢失。
- 落库前先补齐 tool_use 断口：打断会把工具循环截断在中间，直接落库会把
  「有 tool_use 无 tool_result」的孤儿写进库（对照 agent.run_turn 轮首的
  repair_dangling_tool_uses）。
"""

from __future__ import annotations

import asyncio
import io

import pytest
from conftest import FakeProvider
from pydantic import BaseModel
from rich.console import Console

from skysheep.cli.app import ChatApp
from skysheep.cli.render import Renderer
from skysheep.core.agent import Agent
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.session.store import SessionStore
from skysheep.tools import ToolRegistry
from skysheep.tools.base import Safety, Tool

# ---- 测试桩 ----


class _FakePrompt:
    """按队列回放的 PromptSession 桩：队列耗尽后抛 EOFError（= Ctrl-D 退出）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    async def prompt_async(self, *args, **kwargs) -> str:
        if not self.replies:
            raise EOFError
        return self.replies.pop(0)


class _HangStreamProvider(FakeProvider):
    """第一次 stream 调用挂起：模拟流式输出进行中被 Ctrl-C 打断。"""

    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append(list(messages))
        self.started.set()
        await asyncio.Event().wait()  # 永不 set：挂到被取消为止
        yield  # pragma: no cover - 被取消后到不了这里


class _InterruptingProvider(FakeProvider):
    """第二次 stream 调用直接抛 KeyboardInterrupt：模拟信号路径的打断。"""

    def __init__(self, scripted: list[list]) -> None:
        super().__init__(scripted)
        self.n_calls = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.n_calls += 1
        if self.n_calls >= 2:
            raise KeyboardInterrupt
        async for pe in super().stream(messages, tool_schemas, effort=effort):
            yield pe


class _HangArgs(BaseModel):
    pass


class _HangTool(Tool):
    """测试用只读工具：执行中挂起，模拟长耗时工具被 Ctrl-C 打断。"""

    name = "hang_tool"
    description = "测试用：一直挂起直到被打断"
    safety = Safety.READONLY
    args_model = _HangArgs

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, args, ctx):
        self.started.set()
        await asyncio.Event().wait()  # 永不 set：挂到被取消为止
        return ""  # pragma: no cover - 被取消后到不了这里


async def _make_app(tmp_path, provider, replies, tools=()) -> ChatApp:
    """只装 run() 用到的部件（绕过 setup() 的完整装配）。"""
    (tmp_path / "proj").mkdir(exist_ok=True)
    app = object.__new__(ChatApp)
    app.console = Console(file=io.StringIO(), force_terminal=False, width=200)
    app.renderer = Renderer(app.console)
    app.store = await SessionStore(tmp_path / "cli.db").connect()
    app.project = await app.store.get_or_create_project(str(tmp_path / "proj"))
    app.session = await app.store.create_session(app.project.id)
    app.agent = Agent(
        provider=provider,
        registry=ToolRegistry(list(tools)),
        gate=PermissionGate(store=None, project_id=None),
        working_dir=tmp_path / "proj",
    )
    app.agent.set_system("system")
    app.prompt = _FakePrompt(replies)
    return app


async def _load_db(tmp_path, session_id):
    store = await SessionStore(tmp_path / "cli.db").connect()
    try:
        return await store.load_messages(session_id)
    finally:
        await store.close()


def _assert_no_dangling_tool_use(msgs) -> None:
    """库里每个 tool_use 都必须有配对的 tool_result（发现 10 的断口要求）。"""
    got: set[str] = set()
    for m in msgs:
        if m.role == "tool":
            for blk in m.content:
                got.add(blk.tool_use_id)
    for m in msgs:
        if m.role != "assistant":
            continue
        for blk in m.content:
            if getattr(blk, "id", None) and blk.type == "tool_use":
                assert blk.id in got, f"tool_use {blk.id} 落库后没有配对的 tool_result"


# ---- 用例 ----


async def test_cancelled_error_interrupt_persists_turn(tmp_path):
    """轮中 Ctrl-C（Windows + py3.11+ 实际形态 = CancelledError）：
    REPL 活下来并出现「已打断」，本轮用户消息落库。"""
    provider = _HangStreamProvider()
    app = await _make_app(tmp_path, provider, ["你好"])
    repl = asyncio.create_task(app.run())
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    # 模拟 asyncio.Runner 对第一次 Ctrl-C 的处理：cancel 主任务
    repl.cancel()
    try:
        await asyncio.wait_for(repl, timeout=5)
    except asyncio.CancelledError:
        pytest.fail("轮中 CancelledError 穿透了打断分支，REPL 直接退出（发现 10 旧缺陷）")
    assert "已打断" in app.console.file.getvalue()
    msgs = await _load_db(tmp_path, app.session.id)
    assert len(msgs) == 1 and msgs[0].role == "user", "打断轮的用户消息应落库"
    assert "你好" in msgs[0].text


async def test_cancelled_error_mid_tool_repairs_dangling_tool_use(tmp_path):
    """轮中工具执行到一半被打断：落库前补齐 tool_use 断口，库里不留孤儿。"""
    provider = FakeProvider([[ToolUseBlock(id="tu1", name="hang_tool", input={})]])
    hang = _HangTool()
    app = await _make_app(tmp_path, provider, ["跑个工具"], tools=[hang])
    repl = asyncio.create_task(app.run())
    await asyncio.wait_for(hang.started.wait(), timeout=5)
    repl.cancel()
    try:
        await asyncio.wait_for(repl, timeout=5)
    except asyncio.CancelledError:
        pytest.fail("轮中 CancelledError 穿透了打断分支，REPL 直接退出（发现 10 旧缺陷）")
    assert "已打断" in app.console.file.getvalue()
    msgs = await _load_db(tmp_path, app.session.id)
    _assert_no_dangling_tool_use(msgs)
    assert [m.role for m in msgs] == ["user", "assistant", "tool"]
    fixed = [b for b in msgs[2].content if b.tool_use_id == "tu1"]
    assert fixed and fixed[0].is_error, "补上的断口结果应标注 is_error（中断说明）"


async def test_keyboard_interrupt_persists_interrupted_turn(tmp_path):
    """KeyboardInterrupt 路径：打断分支可达，打断轮的用户消息同样落库；
    正常轮的增量落库行为不变。"""
    provider = _InterruptingProvider([[TextBlock(text="第一轮完整回答")]])
    app = await _make_app(tmp_path, provider, ["第一问", "第二问"])
    await asyncio.wait_for(app.run(), timeout=10)
    assert "已打断" in app.console.file.getvalue()
    msgs = await _load_db(tmp_path, app.session.id)
    assert any(m.role == "assistant" and "第一轮完整回答" in m.text for m in msgs), \
        "正常轮落库不应被改动破坏"
    assert any(m.role == "user" and "第二问" in m.text for m in msgs), \
        "打断轮（KeyboardInterrupt）的用户消息应落库"
    _assert_no_dangling_tool_use(msgs)
