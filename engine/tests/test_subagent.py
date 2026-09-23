"""子代理测试：explore 同步、后台任务、自动拒绝门控。"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.core.subagent import SubagentGate, TaskManager
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import Decision
from skysheep.tools import WriteFileTool


def make_main_agent(tmp_path, main_script, sub_script):
    """构造主 Agent + spawn_agent/check_task 工具。"""
    from skysheep.core import Agent
    from skysheep.core.subagent import CheckTaskTool, SpawnAgentTool
    from skysheep.security.gate import PermissionGate
    from skysheep.tools import ToolRegistry, default_tools

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider(list(sub_script)),
        working_dir=tmp_path,
    )
    registry = ToolRegistry(default_tools())
    registry.register(SpawnAgentTool(tasks))
    registry.register(CheckTaskTool(tasks))
    agent = Agent(
        provider=FakeProvider(main_script),
        registry=registry,
        gate=PermissionGate(),
        working_dir=tmp_path,
        max_iterations=10,
    )
    return agent, tasks


async def test_explore_sync_returns_report(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    main_script = [
        [ToolUseBlock(id="c1", name="spawn_agent", input={
            "agent_type": "explore", "prompt": "列出目录里有什么文件",
        })],
        [TextBlock(text="子代理说目录里有 a.txt")],
    ]
    sub_script = [[TextBlock(text="REPORT: 目录中有 1 个文件: a.txt")]]
    agent, _tasks = make_main_agent(tmp_path, main_script, sub_script)

    finished = []
    async for ev in agent.run_turn("派个子代理看看目录"):
        if ev.kind == "tool_call_finished":
            finished.append(ev)

    assert finished and not finished[0].is_error
    assert "REPORT" in finished[0].preview
    assert "a.txt" in finished[0].preview
    # 无需任何确认（spawn_agent 只读）
    roles = [m.role for m in agent.history]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_background_task_and_check(tmp_path):
    sub_script = [[TextBlock(text="BG-RESULT: 调研完成")]]
    tasks = TaskManager(provider_factory=lambda: FakeProvider(list(sub_script)), working_dir=tmp_path)
    task_id = tasks.start_background("explore", "调研一下")
    # 轮询直到完成
    for _ in range(100):
        rec = tasks.status(task_id)
        if rec.status == "done":
            break
        await asyncio.sleep(0.02)
    assert rec.status == "done"
    assert "BG-RESULT" in rec.result

    # check_task 工具查询
    from skysheep.core.subagent import CheckTaskTool
    from skysheep.tools.base import ToolContext

    tool = CheckTaskTool(tasks)
    out = await tool.run(tool.args_model(task_id=task_id), ToolContext(working_dir=tmp_path))
    assert "done" in out and "BG-RESULT" in out
    from skysheep.tools.base import ToolError

    with pytest.raises(ToolError, match="unknown task_id"):
        await tool.run(tool.args_model(task_id="nonexistent"), ToolContext(working_dir=tmp_path))


async def test_subagent_gate_auto_denies_write(tmp_path):
    # 子代理尝试写文件 → 被 SubagentGate 自动拒绝 → 在报告中说明无法写
    sub_script = [
        [ToolUseBlock(id="w1", name="write_file", input={"path": "x.txt", "content": "nope"})],
        [TextBlock(text="REPORT: 写文件被拒绝（子代理无确认通道），文件未被创建。")],
    ]
    tasks = TaskManager(provider_factory=lambda: FakeProvider(list(sub_script)), working_dir=tmp_path)
    report = await tasks.run_sync("task", "写个文件")
    assert "拒绝" in report
    assert not (tmp_path / "x.txt").exists()


async def test_subagent_gate_unit():
    gate = SubagentGate()
    assert await gate.authorize(ReadTool(), {}) is None  # 只读放行
    pending = await gate.authorize(WriteFileTool(), {"path": "x"})
    assert pending is not None
    assert await pending.wait() == Decision.DENY  # 已预拒绝，不阻塞
    # deny_note 把话说死：模型不再反复重试写操作
    assert "不要重试" in pending.deny_note
    assert "报告" in pending.deny_note


class ReadTool:
    """最小只读工具桩（authorize 只看 safety）。"""

    name = "read_file"
    safety = None

    def __init__(self) -> None:
        from skysheep.tools import Safety

        self.safety = Safety.READONLY

    def arg_text(self, input_dict):
        return "{}"


# ---- 子代理定义存储 ----


def test_subagent_store_roundtrip(tmp_path):
    from skysheep.core.subagent_store import SubagentDef, SubagentStore

    path = tmp_path / "subagents.json"
    store = SubagentStore(path)
    store.load()
    assert set(store.builtin) == {
        "task", "explore", "reviewer", "researcher", "writer", "planner",
    }
    store.upsert_custom(SubagentDef(
        name="repo-auditor", description="审计仓库", prompt="先列目录",
        tools=["read_file", "grep"], provider="zhipu", model="glm-5.3",
    ))
    store.set_override("explore", provider="deepseek", model="deepseek-chat", reasoning="high")

    again = SubagentStore(path)
    again.load()
    d = again.get_custom("repo-auditor")
    assert d is not None and d.tools == ["read_file", "grep"] and d.provider == "zhipu"
    assert again.builtin["explore"].reasoning == "high"
    # 同名再存 = 更新而不是追加
    again.upsert_custom(d.model_copy(update={"description": "改过的描述"}))
    assert len(again.custom) == 1
    assert again.get_custom("repo-auditor").description == "改过的描述"
    # 删除
    assert again.remove_custom("repo-auditor") is True
    assert again.custom == []


def test_subagent_store_bad_inputs(tmp_path):
    from skysheep.core.subagent_store import SubagentDefError, SubagentStore, validate_subagent_name

    store = SubagentStore(tmp_path / "s.json")
    store.load()
    with pytest.raises(SubagentDefError):
        validate_subagent_name("explore")  # 内置名保留
    with pytest.raises(SubagentDefError):
        validate_subagent_name("有中文")
    with pytest.raises(SubagentDefError):
        validate_subagent_name("")
    assert validate_subagent_name("repo-auditor") == "repo-auditor"
    # 非法思考强度
    with pytest.raises(SubagentDefError):
        store.set_override("explore", provider="", model="", reasoning="turbo")
    with pytest.raises(SubagentDefError):
        store.set_override("nope", provider="", model="", reasoning="")
    with pytest.raises(SubagentDefError):
        store.remove_custom("ghost")
    # 坏文件不能让启动崩：load 静默回默认
    (tmp_path / "s.json").write_text("{ not json", encoding="utf-8")
    store.load()
    assert store.custom == [] and store.builtin["task"].provider == ""


async def test_custom_subagent_runs_with_own_tools_and_prompt(tmp_path):
    """自定义子代理：只拿到勾选工具 + 专项指令进入系统提示词。"""
    from skysheep.core.subagent import SpawnAgentTool, TaskManager
    from skysheep.core.subagent_store import SubagentDef, SubagentStore
    from skysheep.tools import ToolRegistry, default_tools
    from skysheep.tools.base import ToolContext

    store = SubagentStore(tmp_path / "s.json")
    store.load()
    store.upsert_custom(SubagentDef(
        name="auditor", description="审计仓库结构", prompt="务必输出风险清单",
        tools=["glob"],  # 只要 glob：read_file 都拿不到
    ))
    seen: dict = {}

    def registry_resolver(policy):
        base = ToolRegistry(default_tools())
        tools = [t for t in base.all() if isinstance(policy, list) and t.name in policy]
        seen["tools"] = [t.name for t in tools]
        return ToolRegistry(tools)

    captured: dict = {}

    def provider_resolver(provider, model, reasoning):
        captured["provider"] = (provider, model, reasoning)
        return FakeProvider([[TextBlock(text="REPORT: 审计完成")]])

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="default")]]),
        working_dir=tmp_path,
        store=store,
        provider_resolver=provider_resolver,
        registry_resolver=registry_resolver,
    )
    assert tasks.known_agent_type("auditor") is True
    assert tasks.known_agent_type("ghost") is False
    assert tasks.list_custom() == [("auditor", "审计仓库结构")]

    report = await tasks.run_sync("auditor", "审计一下")
    assert "REPORT" in report
    assert seen["tools"] == ["glob"]
    assert captured["provider"] == ("", "", "")

    # spawn_agent 的说明文字里点名了自定义子代理，且拒绝未知类型
    tool = SpawnAgentTool(tasks)
    assert "auditor" in tool.description
    with pytest.raises(Exception, match="未知的子代理类型"):
        await tool.run(
            tool.args_model(agent_type="ghost", prompt="x"),
            ToolContext(working_dir=tmp_path),
        )


async def test_custom_subagent_disabled_and_deleted(tmp_path):
    from skysheep.core.subagent import TaskManager
    from skysheep.core.subagent_store import SubagentDef, SubagentStore

    store = SubagentStore(tmp_path / "s.json")
    store.load()
    store.upsert_custom(SubagentDef(name="temp", enabled=False))
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([]),
        working_dir=tmp_path,
        store=store,
    )
    # 停用的定义不算已知类型，也不会出现在清单里
    assert tasks.known_agent_type("temp") is False
    assert tasks.list_custom() == []
    store.upsert_custom(SubagentDef(name="temp", enabled=True))
    assert tasks.known_agent_type("temp") is True


async def test_builtin_override_uses_specified_provider(tmp_path):
    """内置子代理指定了模型 → 走 provider_resolver；留空 → 走默认 factory。"""
    from skysheep.core.subagent import TaskManager
    from skysheep.core.subagent_store import SubagentStore

    store = SubagentStore(tmp_path / "s.json")
    store.load()
    calls: list = []

    def provider_resolver(provider, model, reasoning):
        calls.append((provider, model, reasoning))
        return FakeProvider([[TextBlock(text="REPORT: 用指定模型跑的")]])

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="REPORT: 默认模型")]]),
        working_dir=tmp_path,
        store=store,
        provider_resolver=provider_resolver,
    )
    # 没有覆盖 → 不调用 resolver
    out = await tasks.run_sync("explore", "看看")
    assert "默认模型" in out and calls == []

    # 设置覆盖后 → 走 resolver
    store.set_override("explore", provider="zhipu", model="glm-5.3", reasoning="high")
    out2 = await tasks.run_sync("explore", "看看")
    assert "指定模型" in out2
    assert calls == [("zhipu", "glm-5.3", "high")]


async def test_subagent_provider_resolver_failure_is_reported(tmp_path):
    """定义指向的服务用不了（如没配 Key）→ 任务记为 error，不能崩主流程。"""
    from skysheep.core.subagent import TaskManager
    from skysheep.core.subagent_store import SubagentDef, SubagentStore

    store = SubagentStore(tmp_path / "s.json")
    store.load()
    store.upsert_custom(SubagentDef(name="broken", tools="readonly"))

    def boom(provider, model, reasoning):
        raise RuntimeError("unknown provider: nope")

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([]),
        working_dir=tmp_path,
        store=store,
        provider_resolver=boom,
        registry_resolver=lambda p: None,
    )
    with pytest.raises(RuntimeError, match="启动失败"):
        await tasks.run_sync("broken", "跑一下")


# ---- 记账 / 取消 / 并发护栏 / 直播 / 终态广播 / 详情 ----


class SlowProvider(FakeProvider):
    """永不一样的慢模型：stream 挂起不结束，用来测真取消。"""

    def __init__(self, scripted: list[list] | None = None) -> None:
        super().__init__(scripted or [])

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append(list(messages))
        await asyncio.sleep(30)
        yield  # pragma: no cover - 取消后走不到这里


async def _wait_status(tasks, task_id, status, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        rec = tasks.status(task_id)
        if rec.status == status:
            return rec
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务未在 {timeout}s 内变为 {status}（当前 {rec.status}）")


async def test_usage_recorded_for_subagent(tmp_path):
    """子代理烧掉的 token 要入账：归属派生会话、带子代理自己的 provider/模型。"""
    from skysheep.core.subagent import TaskManager

    recorded: list[tuple] = []

    async def recorder(session_id, provider, model, in_tok, out_tok, cached_tok=0):
        recorded.append((session_id, provider, model, in_tok, out_tok, cached_tok))

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="REPORT: ok")]]),
        working_dir=tmp_path,
        usage_recorder=recorder,
    )
    report = await tasks.run_sync("explore", "看看", session_id="sess-1")
    assert "REPORT" in report
    assert recorded and recorded[0][0] == "sess-1"
    assert recorded[0][1] == "fake" and recorded[0][2] == "fake-1"
    assert recorded[0][3] > 0 and recorded[0][4] > 0
    assert recorded[0][5] >= 0  # 缓存命中数随记账透传（Fake 不报明细时为 0）
    rec = list(tasks._tasks.values())[0]
    assert rec.tokens_in > 0 and rec.tokens_out > 0


async def test_cancel_all_really_cancels_background(tmp_path):
    """「全部取消」要真正终止后台任务（此前只改状态字段，任务照跑照烧钱）。"""
    import contextlib

    from skysheep.core.subagent import TaskManager

    tasks = TaskManager(
        provider_factory=SlowProvider,
        working_dir=tmp_path,
    )
    task_id = tasks.start_background("explore", "慢慢调研")
    await _wait_status(tasks, task_id, "running")
    tasks.cancel_all()
    rec = tasks.status(task_id)
    # 任务可能尚未被调度就被取消（await 会抛 CancelledError），两种路径都算已取消
    with contextlib.suppress(asyncio.CancelledError):
        await rec.asyncio_task
    assert rec.status == "cancelled"
    assert "取消" in (rec.error or "")


async def test_concurrency_limit_blocks_extra_spawns(tmp_path):
    """并发护栏：同步派生满员 → 友好报错；后台派生满员 → 自动排队不报错。"""
    import contextlib

    from skysheep.core.subagent import SubagentLimitError, TaskManager
    from skysheep.tools.base import ToolContext, ToolError

    tasks = TaskManager(
        provider_factory=SlowProvider,
        working_dir=tmp_path,
        max_concurrent=1,
    )
    first = tasks.start_background("explore", "第一个")
    assert tasks.status(first).status == "running"
    # 后台满员 → 排队（不报错），稍后有空位自动开跑
    second = tasks.start_background("explore", "第二个")
    assert tasks.status(second).status == "queued"
    # 同步派生满员 → 报错（模型要等或改后台）
    with pytest.raises(SubagentLimitError, match="上限"):
        await tasks.run_sync("explore", "同步的也算")
    # SpawnAgentTool 把它转成 ToolError 返回给模型
    from skysheep.core.subagent import SpawnAgentTool

    tool = SpawnAgentTool(tasks)
    with pytest.raises(ToolError, match="上限"):
        await tool.run(
            tool.args_model(agent_type="explore", prompt="x"),
            ToolContext(working_dir=tmp_path),
        )
    # 取消全部：排队的也要落终态，不留幽灵
    tasks.cancel_all()
    assert tasks.status(second).status == "cancelled"
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.status(first).asyncio_task
    assert tasks.status(first).status == "cancelled"


async def test_background_queue_auto_promotes(tmp_path):
    """并发空位出来后，排队的后台任务按序自动开跑（补位成功的标志：转为 running）。"""
    import contextlib

    from skysheep.core.subagent import TaskManager

    tasks = TaskManager(provider_factory=SlowProvider, working_dir=tmp_path, max_concurrent=1)
    first = tasks.start_background("explore", "占位")
    await _wait_status(tasks, first, "running")
    second = tasks.start_background("explore", "排队")
    assert tasks.status(second).status == "queued"
    tasks.cancel_task(first)  # 只取消占位任务 → 空位 → 排队任务自动补位
    await _wait_status(tasks, second, "running")
    # 收尾：把补位的也取消掉
    assert tasks.cancel_task(second) is True
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.status(second).asyncio_task
    assert tasks.status(first).status == "cancelled"
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.status(first).asyncio_task


async def test_run_sync_cancel_leaves_no_zombie(tmp_path):
    """点「停止」取消主轮时，同步子代理必须落终态——否则僵尸 running
    永久占并发名额，几次停止之后再也派不出子代理（只能重启）。"""
    import contextlib

    from skysheep.core.subagent import TaskManager

    tasks = TaskManager(provider_factory=SlowProvider, working_dir=tmp_path, max_concurrent=1)
    runner = asyncio.create_task(tasks.run_sync("explore", "慢慢跑"))
    task_id = ""
    for _ in range(200):
        running = [r for r in tasks._tasks.values() if r.status == "running"]
        if running and running[0].duration_s > 0:
            task_id = running[0].id
            break
        await asyncio.sleep(0.01)
    assert task_id, "任务没有真正开跑"
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    rec = tasks.status(task_id)
    assert rec.status == "cancelled" and "取消" in (rec.error or "")
    # 并发名额已释放：能立即再派且直接运行（而不是被僵尸挡成报错/排队）
    new_id = tasks.start_background("explore", "再来一个")
    assert tasks.status(new_id).status == "running"
    tasks.cancel_all()
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.status(new_id).asyncio_task


async def test_check_task_rejects_other_session(tmp_path):
    """归属校验（B13 同款）：别的会话派生的任务，本会话的模型不能取报告。"""
    from skysheep.core.subagent import CheckTaskTool, TaskManager
    from skysheep.tools.base import ToolContext, ToolError

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="BG-SECRET")]]),
        working_dir=tmp_path,
    )
    task_id = tasks.start_background("explore", "看看", session_id="sess-A")
    await _wait_status(tasks, task_id, "done")
    tool = CheckTaskTool(tasks)
    # 同会话正常取（归属随 ctx.session_id 走，不再依赖全局「当前会话」指针）
    assert "BG-SECRET" in await tool.run(
        tool.args_model(task_id=task_id),
        ToolContext(working_dir=tmp_path, session_id="sess-A"),
    )
    # 换会话后不可取（也不泄露存在性：同样报 unknown task_id）
    with pytest.raises(ToolError, match="unknown task_id"):
        await tool.run(
            tool.args_model(task_id=task_id),
            ToolContext(working_dir=tmp_path, session_id="sess-B"),
        )


async def test_spawn_tool_attributes_via_ctx_session(tmp_path):
    """spawn_agent 从 ctx.session_id 归属任务：并行轮各自归属，无全局「当前会话」指针。"""
    from skysheep.core.subagent import SpawnAgentTool, TaskManager
    from skysheep.tools.base import ToolContext

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="R")]]),
        working_dir=tmp_path,
    )
    tool = SpawnAgentTool(tasks)
    out = await tool.run(
        tool.args_model(agent_type="explore", prompt="看看", background=True),
        ToolContext(working_dir=tmp_path, session_id="sess-Z"),
    )
    task_id = out.split("task_id=")[1].split(";")[0]
    await _wait_status(tasks, task_id, "done")
    assert tasks.get_detail(task_id, session_id="sess-Z") is not None
    assert tasks.get_detail(task_id, session_id="other") is None
    # 用量也记到派生会话名下
    assert tasks.status(task_id).session_id == "sess-Z"


async def test_parallel_sessions_keep_own_attribution(tmp_path):
    """两个会话交错派生：用量与任务簿归属不串台（修复前共享单指针会互盖）。"""
    recorded: list[str] = []

    async def recorder(session_id, provider, model, in_tok, out_tok, cached_tok=0):
        recorded.append(session_id)

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="R")]]),
        working_dir=tmp_path,
        usage_recorder=recorder,
        max_concurrent=2,
    )
    a = tasks.start_background("explore", "A 的任务", session_id="sess-A")
    b = tasks.start_background("explore", "B 的任务", session_id="sess-B")
    await _wait_status(tasks, a, "done")
    await _wait_status(tasks, b, "done")
    assert sorted(recorded) == ["sess-A", "sess-B"]
    assert tasks.get_detail(a, session_id="sess-A") is not None
    assert tasks.get_detail(a, session_id="sess-B") is None
    assert tasks.get_detail(b, session_id="sess-B") is not None
    assert tasks.get_detail(b, session_id="sess-A") is None


async def test_wait_task_blocks_until_done_and_dedups(tmp_path):
    """wait_task：等到终态立即返回结果；投递去重与 check_task 共用。"""
    from skysheep.core.subagent import CheckTaskTool, TaskManager, WaitTaskTool
    from skysheep.tools.base import ToolContext

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="WAIT-OK")]]),
        working_dir=tmp_path,
    )
    task_id = tasks.start_background("explore", "跑一下")
    tool = WaitTaskTool(tasks)
    ctx = ToolContext(working_dir=tmp_path)
    out = await tool.run(tool.args_model(task_id=task_id, timeout_seconds=5), ctx)
    assert "done" in out and "WAIT-OK" in out
    # 报告已投递：再查只回显开头
    check = CheckTaskTool(tasks)
    again = await check.run(check.args_model(task_id=task_id), ctx)
    assert "已在此前的查询里投递" in again and "WAIT-OK" in again


async def test_wait_task_timeout_returns_running(tmp_path):
    """wait_task 超时：不报错，返回当前状态让模型决定继续等或先干别的。"""
    import contextlib

    from skysheep.core.subagent import TaskManager, WaitTaskTool
    from skysheep.tools.base import ToolContext

    tasks = TaskManager(provider_factory=SlowProvider, working_dir=tmp_path)
    task_id = tasks.start_background("explore", "慢活")
    tool = WaitTaskTool(tasks)
    out = await tool.run(tool.args_model(task_id=task_id, timeout_seconds=1),
                         ToolContext(working_dir=tmp_path))
    assert "超时" in out and "running" in out
    tasks.cancel_all()
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.status(task_id).asyncio_task


async def test_long_report_written_to_file_and_excerpted(tmp_path):
    """长报告落盘 .skysheep/reports/，投递只带路径 + 摘录；短报告保持内联。"""
    from pathlib import Path

    from skysheep.core.subagent import TaskManager

    long_text = "很长的报告内容" * 500  # 远超内联阈值
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text=long_text)]]),
        working_dir=tmp_path,
    )
    delivered = await tasks.run_sync("explore", "写个长报告")
    assert len(delivered) < len(long_text)  # 投的是摘录
    assert ".skysheep" in delivered and "reports" in delivered
    rec = next(iter(tasks._tasks.values()))
    assert rec.report_path
    p = Path(rec.report_path)
    assert p.exists() and p.read_text(encoding="utf-8") == long_text

    # 短报告：不落盘，整份内联（行为不变）
    tasks2 = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="短报告")]]),
        working_dir=tmp_path,
    )
    out2 = await tasks2.run_sync("explore", "短的")
    assert out2 == "短报告"
    assert next(iter(tasks2._tasks.values())).report_path == ""


def test_task_book_persists_and_marks_interrupted(tmp_path):
    """任务簿持久化：终态记录重启后仍可查；running 重启即标中断，不留假运行。"""
    from skysheep.core.subagent import TaskManager

    state = tmp_path / "subagent_tasks.json"
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="x")]]),
        working_dir=tmp_path, state_path=state,
    )
    done = tasks._new_record("explore", "完成的")
    done.status = "done"
    done.result = "结果在这里"
    done.finished_at = 1.0
    running = tasks._new_record("explore", "跑一半的")
    running.status = "running"
    tasks._persist()

    again = TaskManager(
        provider_factory=lambda: FakeProvider([]),
        working_dir=tmp_path, state_path=state,
    )
    assert again.status(done.id).status == "done"
    assert again.status(done.id).result == "结果在这里"
    r2 = again.status(running.id)
    assert r2.status == "error" and "重启" in (r2.error or "")


async def test_spawn_announce_precedes_stream(tmp_path):
    """subagent_spawned 广播先于任务过程事件（前端靠它绑定直播卡片）。"""
    from skysheep.core.subagent import TaskManager

    seen: list[dict] = []
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="R")]]),
        working_dir=tmp_path,
        event_emitter=lambda ev: seen.append(ev),
    )
    await tasks.run_sync("explore", "看看", session_id="sess-1")
    assert seen[0]["kind"] == "subagent_spawned"
    assert seen[0]["session_id"] == "sess-1"
    assert seen[0]["background"] is False and seen[0]["task_id"]
    kinds = [s["kind"] for s in seen]
    assert "subagent_event" in kinds

    seen.clear()
    tid = tasks.start_background("explore", "后台的")
    await _wait_status(tasks, tid, "done")
    sp = [s for s in seen if s["kind"] == "subagent_spawned"]
    assert sp and sp[0]["background"] is True and sp[0]["task_id"] == tid


async def test_turn_note_injected_then_consumed(tmp_path):
    """后台任务完成 → 记注记；下一轮注入；报告被 check_task 取走后不再提示。"""
    from skysheep.core.subagent import CheckTaskTool, TaskManager
    from skysheep.tools.base import ToolContext

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="BG")]]),
        working_dir=tmp_path,
    )
    tid = tasks.start_background("explore", "后台活", session_id="sess-1")
    await _wait_status(tasks, tid, "done")

    note = tasks.pop_turn_note("sess-1")
    assert tid in note and "check_task" in note
    assert tasks.pop_turn_note("sess-1") == ""  # 取走即清

    # 报告已被模型取走 → 不再注入过期提示
    tid2 = tasks.start_background("explore", "取过的", session_id="sess-1")
    await _wait_status(tasks, tid2, "done")
    tool = CheckTaskTool(tasks)
    await tool.run(tool.args_model(task_id=tid2),
                   ToolContext(working_dir=tmp_path, session_id="sess-1"))
    assert tasks.pop_turn_note("sess-1") == ""


async def test_cancel_single_task(tmp_path):
    """单任务取消：排队中直接落终态；运行中的真取消。"""
    import contextlib

    from skysheep.core.subagent import TaskManager

    tasks = TaskManager(provider_factory=SlowProvider, working_dir=tmp_path, max_concurrent=1)
    first = tasks.start_background("explore", "运行中的")
    await _wait_status(tasks, first, "running")
    second = tasks.start_background("explore", "排队的")
    assert tasks.status(second).status == "queued"
    # 排队中的：直接落终态并移出队列
    assert tasks.cancel_task(second) is True
    assert tasks.status(second).status == "cancelled"
    # 运行中的：真取消
    assert tasks.cancel_task(first) is True
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.status(first).asyncio_task
    assert tasks.status(first).status == "cancelled"
    # 终态任务不可再取消；未知 id 返回 False
    assert tasks.cancel_task(first) is False
    assert tasks.cancel_task("ghost") is False


def test_subagent_all_policy_excludes_computer_tools(tmp_path):
    """tools="all" 不含电脑控制七件套；勾选模式仍可显式点名。"""
    from types import SimpleNamespace

    from skysheep.server.backend import ServerBackend
    from skysheep.tools import MouseTool, ReadFileTool, ScreenshotTool, ToolRegistry, WriteFileTool

    registry = ToolRegistry([ReadFileTool(), WriteFileTool(), ScreenshotTool(), MouseTool()])
    be = object.__new__(ServerBackend)
    be._base_agent = SimpleNamespace(registry=registry)

    all_names = {t.name for t in be._subagent_registry("all").all()}
    assert "write_file" in all_names and "read_file" in all_names
    assert "screenshot" not in all_names and "mouse" not in all_names

    ro_names = {t.name for t in be._subagent_registry("readonly").all()}
    assert ro_names == {"read_file"}  # 只读策略同样不含电脑控制

    # 勾选模式是用户显式点名：允许给 screenshot
    pick_names = {t.name for t in be._subagent_registry(["screenshot"]).all()}
    assert pick_names == {"screenshot"}


async def test_custom_subagent_reasoning_reaches_resolver(tmp_path):
    """自定义子代理的思考强度要传给 provider 解析器（与内置覆盖同语义）。"""
    from skysheep.core.subagent import TaskManager
    from skysheep.core.subagent_store import SubagentDef, SubagentStore

    store = SubagentStore(tmp_path / "s.json")
    store.load()
    store.upsert_custom(SubagentDef(name="thinker", reasoning="high"))
    calls: list[tuple] = []

    def resolver(provider, model, reasoning):
        calls.append((provider, model, reasoning))
        return FakeProvider([[TextBlock(text="ok")]])

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="default")]]),
        working_dir=tmp_path, store=store, provider_resolver=resolver,
        registry_resolver=lambda policy: None,
    )
    await tasks.run_sync("thinker", "跑")
    assert calls == [("", "", "high")]


async def test_subagent_events_forwarded_filtered(tmp_path):
    """直播：过程事件转发给宿主 emitter；permission_request 不转发（已预拒绝）。"""
    from skysheep.core.subagent import TaskManager

    sub_script = [
        [ToolUseBlock(id="r1", name="list_dir", input={"path": "."})],
        [TextBlock(text="REPORT: 直播结束")],
    ]
    seen: list[dict] = []

    def emitter(ev: dict) -> None:
        seen.append(ev)

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider(list(sub_script)),
        working_dir=tmp_path,
        event_emitter=emitter,
    )
    report = await tasks.run_sync("explore", "列目录")
    assert "REPORT" in report
    events = [s for s in seen if s["kind"] == "subagent_event"]
    assert events, "没有转发任何过程事件"
    kinds = [s["event"]["kind"] for s in events]
    assert "tool_call_started" in kinds and "tool_call_finished" in kinds
    assert "text_delta" in kinds and "assistant_message" in kinds
    assert "permission_request" not in kinds
    # spawned 广播在最前（前端靠它绑定直播卡片），带任务与类型
    assert seen[0]["kind"] == "subagent_spawned"
    assert seen[0]["agent_type"] == "explore" and seen[0]["task_id"]
    first = events[0]
    assert first["kind"] == "subagent_event"
    assert first["agent_type"] == "explore" and first["task_id"]
    # 工具调用事件带名字与参数，前端直播行靠它渲染
    started = next(s for s in events if s["event"]["kind"] == "tool_call_started")
    assert started["event"]["name"] == "list_dir"


async def test_task_finished_broadcast_on_background(tmp_path):
    """后台任务终态广播 task_finished（前端弹通知 + 刷新任务簿）。"""
    from skysheep.core.subagent import TaskManager

    seen: list[dict] = []

    def emitter(ev: dict) -> None:
        seen.append(ev)

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="BG: done")]]),
        working_dir=tmp_path,
        event_emitter=emitter,
    )
    task_id = tasks.start_background("explore", "后台调研")
    await _wait_status(tasks, task_id, "done")
    finished = [s for s in seen if s["kind"] == "task_finished"]
    assert finished and finished[-1]["task_id"] == task_id
    assert finished[-1]["status"] == "done"
    assert finished[-1]["agent_type"] == "explore"


async def test_get_detail_returns_full_record(tmp_path):
    """详情：完整 prompt/报告不截断，带 provider 与 token 信息。"""
    from skysheep.core.subagent import TaskManager

    long_result = "R" * 5000
    long_prompt = "P" * 5000
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text=long_result)]]),
        working_dir=tmp_path,
    )
    task_id = tasks.start_background("explore", long_prompt)
    await _wait_status(tasks, task_id, "done")
    detail = tasks.get_detail(task_id)
    assert detail["prompt"] == long_prompt
    assert detail["result"] == long_result
    assert detail["provider"] == "fake / fake-1"
    assert detail["tokens_in"] > 0
    # 列表快照仍是截断版
    snap = tasks.list_tasks()[0]
    assert len(snap["prompt"]) == 160 and len(snap["result"]) == 800
    assert tasks.get_detail("nope") is None


async def test_tasks_get_backend_method(tmp_path):
    """backend.tasks_get 返回详情；未知 id 报错（WS 层转 ok=false）。"""
    from skysheep.core.subagent import TaskManager
    from skysheep.server.backend import ServerBackend

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="x")]]),
        working_dir=tmp_path,
    )
    task_id = tasks.start_background("explore", "看看")
    await _wait_status(tasks, task_id, "done")

    be = object.__new__(ServerBackend)  # 只验证方法逻辑，不跑完整启动
    be.tasks = tasks
    out = await be.tasks_get({"task_id": task_id})
    assert out["task"]["id"] == task_id
    try:
        await be.tasks_get({"task_id": "ghost"})
        raised = False
    except ValueError:
        raised = True
    assert raised


async def test_new_builtin_types_registry_and_role_prompt(tmp_path):
    """新增内置型：reviewer/writer/planner 用只读基础集，researcher 额外带联网
    工具；角色提示词要拼进系统消息（模型才知道自己是干嘛的）。"""
    from skysheep.core.subagent import (
        BUILTIN_ROLE_PROMPTS,
        build_subagent_registry,
        run_subagent,
    )
    from skysheep.models.fake import FakeProvider
    from skysheep.tools import WebFetchTool, WebSearchTool

    reg = build_subagent_registry("reviewer")
    assert {t.name for t in reg.all()} == {"read_file", "list_dir", "glob", "grep"}

    web_reg = build_subagent_registry(
        "researcher", [WebSearchTool(), WebFetchTool()]
    )
    assert "web_search" in {t.name for t in web_reg.all()}
    assert "web_fetch" in {t.name for t in web_reg.all()}

    # 未知类型不拼角色词；六个内置型都有角色词或显式为空
    assert BUILTIN_ROLE_PROMPTS.get("ghost", "") == ""
    for t in ("reviewer", "researcher", "writer", "planner"):
        assert BUILTIN_ROLE_PROMPTS.get(t)

    sub_script = [[TextBlock(text="调研完成")]]
    provider = FakeProvider(list(sub_script))
    _, agent = await run_subagent(
        provider=provider,
        working_dir=tmp_path,
        agent_type="researcher",
        prompt="查一下 X",
    )
    system_text = agent.history[0].to_plain()
    assert "联网调研员" in system_text
    assert "read_file" in system_text  # 基础 SUBAGENT_PROMPT 仍在


def test_builtin_override_editable_fields(tmp_path):
    """内置子代理的说明与角色提示词可编辑且持久化；留空 = 回到内置默认。"""
    from skysheep.core.subagent_store import SubagentStore

    path = tmp_path / "subagents.json"
    store = SubagentStore(path)
    store.load()
    store.set_override("reviewer", provider="", model="", reasoning="",
                       description="只审 Python 文件", prompt="按公司规范逐条审查")
    store2 = SubagentStore(path)
    store2.load()
    ov = store2.builtin["reviewer"]
    assert ov.description == "只审 Python 文件"
    assert ov.prompt == "按公司规范逐条审查"
    assert ov.provider == "" and ov.model == ""


def test_builtin_prompt_override_reaches_plan(tmp_path):
    """改写内置型角色提示词后要真正生效：plan 带覆盖词；清空后回到内置默认；
    描述覆盖进 spawn_agent 的说明文字。"""
    from skysheep.core.subagent import TaskManager
    from skysheep.core.subagent_store import SubagentStore

    store = SubagentStore(tmp_path / "subagents.json")
    store.load()
    store.set_override("researcher", provider="", model="", reasoning="",
                       description="只查 arXiv", prompt="按给定清单逐项核对")
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([TextBlock(text="ok")]),
        working_dir=tmp_path, store=store,
    )
    plan = tasks._plan_for("researcher")
    assert plan is not None and plan.role_prompt == "按给定清单逐项核对"

    store.set_override("researcher", provider="", model="", reasoning="",
                       description="只查 arXiv", prompt="")
    plan2 = tasks._plan_for("researcher")
    assert plan2 is not None and "联网调研员" in (plan2.role_prompt or "")

    assert dict(tasks.list_builtin_desc())["researcher"] == "只查 arXiv"
    assert "只查 arXiv" not in dict(tasks.list_builtin_desc())["explore"]


async def test_run_subagent_role_prompt_override(tmp_path):
    """role_prompt 传空串 = 不拼内置角色词（自定义覆盖通道）；None = 内置默认。"""
    from skysheep.core.subagent import run_subagent

    provider = FakeProvider([TextBlock(text="ok")])
    _, agent = await run_subagent(
        provider=provider, working_dir=tmp_path, agent_type="researcher",
        prompt="查一下", role_prompt="自定义角色词",
    )
    system_text = agent.history[0].to_plain()
    assert "自定义角色词" in system_text
    assert "联网调研员" not in system_text
