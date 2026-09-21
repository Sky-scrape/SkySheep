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

    async def recorder(session_id, provider, model, in_tok, out_tok):
        recorded.append((session_id, provider, model, in_tok, out_tok))

    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="REPORT: ok")]]),
        working_dir=tmp_path,
        usage_recorder=recorder,
    )
    tasks.set_active_session("sess-1")
    report = await tasks.run_sync("explore", "看看")
    assert "REPORT" in report
    assert recorded and recorded[0][0] == "sess-1"
    assert recorded[0][1] == "fake" and recorded[0][2] == "fake-1"
    assert recorded[0][3] > 0 and recorded[0][4] > 0
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
    """后台并发到上限后再派 → 友好报错，不是默默继续烧钱。"""
    from skysheep.core.subagent import SubagentLimitError, TaskManager
    from skysheep.tools.base import ToolContext, ToolError

    tasks = TaskManager(
        provider_factory=SlowProvider,
        working_dir=tmp_path,
        max_concurrent=1,
    )
    tasks.start_background("explore", "第一个")
    with pytest.raises(SubagentLimitError, match="上限"):
        tasks.start_background("explore", "第二个")
    with pytest.raises(SubagentLimitError, match="上限"):
        await tasks.run_sync("explore", "同步的也算")
    # SpawnAgentTool 把它转成 ToolError 返回给模型
    from skysheep.core.subagent import SpawnAgentTool

    tool = SpawnAgentTool(tasks)
    with pytest.raises(ToolError, match="上限"):
        await tool.run(
            tool.args_model(agent_type="explore", prompt="x", background=True),
            ToolContext(working_dir=tmp_path),
        )
    tasks.cancel_all()


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
    kinds = [s["event"]["kind"] for s in seen]
    assert "tool_call_started" in kinds and "tool_call_finished" in kinds
    assert "text_delta" in kinds and "assistant_message" in kinds
    assert "permission_request" not in kinds
    first = seen[0]
    assert first["kind"] == "subagent_event"
    assert first["agent_type"] == "explore" and first["task_id"]
    # 工具调用事件带名字与参数，前端直播行靠它渲染
    started = next(s for s in seen if s["event"]["kind"] == "tool_call_started")
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
