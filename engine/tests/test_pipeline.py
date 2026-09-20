"""任务编排测试：存储层 CRUD/依赖物化/环检测、pipeline_write 工具、WS 端到端编排。

端到端覆盖用户的核心场景：并行开发节点 + 依赖它们的审查节点——
审查节点要等上游全部完成才运行，且 prompt 里注入了上游产出。
"""

from __future__ import annotations

import time

import pytest

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.tools.base import ToolError
from skysheep.tools.pipeline import PipelineWriteArgs, PipelineWriteTool

# ---- 存储层：CRUD / 依赖物化 / 环检测 / 重启复位 ----


async def test_pipeline_store_crud_and_dep_materialization(store):
    project = await store.get_or_create_project("/tmp/pl-proj")
    # depends_on 给同批次序号（0 起），落库时物化成真实节点 id
    pipe = await store.add_pipeline(project.id, "登录模块", nodes=[
        {"title": "功能 A", "prompt": "做 A", "depends_on": []},
        {"title": "功能 B", "prompt": "做 B", "depends_on": []},
        {"title": "审查", "prompt": "审查 A 和 B", "depends_on": [0, 1]},
    ], concurrency=2)
    assert pipe["status"] == "draft" and len(pipe["nodes"]) == 3
    a, b, review = pipe["nodes"]
    assert review["depends_on"] == [a["id"], b["id"]], "序号应物化成节点 id"
    assert a["status"] == "blocked"

    # 越界序号宽松丢弃，不挡建线
    pipe2 = await store.add_pipeline(project.id, "越界", nodes=[
        {"title": "唯一", "prompt": "x", "depends_on": [0, 5]},
    ])
    assert pipe2["nodes"][0]["depends_on"] == []

    # update / delete（删节点要摘除他人对它的引用）
    up = await store.update_pipeline_node(review["id"], status="ready")
    assert up["status"] == "ready"
    assert await store.delete_pipeline_node(b["id"]) is True
    nodes = await store.list_pipeline_nodes(pipe["id"])
    assert all(b["id"] not in n["depends_on"] for n in nodes)

    assert await store.delete_pipeline(pipe["id"]) is True
    assert await store.get_pipeline(pipe["id"]) is None
    assert await store.list_pipeline_nodes(pipe["id"]) == []


async def test_pipeline_store_cycle_detection(store):
    project = await store.get_or_create_project("/tmp/pl-cycle")
    pipe = await store.add_pipeline(project.id, "环", nodes=[
        {"title": "A", "prompt": "a", "depends_on": []},
        {"title": "B", "prompt": "b", "depends_on": [0]},
    ])
    a, b = pipe["nodes"]
    # B 依赖 A：再把 A 改成依赖 B 就成环；自引用也算环
    assert await store.has_dependency_cycle(pipe["id"], a["id"], [b["id"]]) is True
    assert await store.has_dependency_cycle(pipe["id"], a["id"], [a["id"]]) is True
    assert await store.has_dependency_cycle(pipe["id"], a["id"], []) is False

    # store 层引用校验：依赖别的流水线的节点要拒绝
    with pytest.raises(ValueError):
        await store._check_dep_refs(pipe["id"], [999], exclude_id=None)


async def test_pipeline_store_reset_interrupted(store):
    project = await store.get_or_create_project("/tmp/pl-reset")
    pipe = await store.add_pipeline(project.id, "中断", nodes=[
        {"title": "在跑", "prompt": "x", "depends_on": []},
        {"title": "就绪", "prompt": "y", "depends_on": [0]},
    ])
    n_run, n_ready = pipe["nodes"]
    await store.update_pipeline_node(n_run["id"], status="running")
    await store.update_pipeline_node(n_ready["id"], status="ready")
    await store.update_pipeline(pipe["id"], status="running")

    fixed = await store.reset_interrupted_pipelines()
    assert fixed == 1
    nodes = {n["title"]: n for n in await store.list_pipeline_nodes(pipe["id"])}
    assert nodes["在跑"]["status"] == "error"
    assert "中断" in nodes["在跑"]["last_error"]
    assert nodes["就绪"]["status"] == "blocked", "就绪态是瞬时的，重启后退回等待"


# ---- 工具：pipeline_write ----


async def test_pipeline_tool_create_list_and_guards(store):
    await store.get_or_create_project("/tmp/pl-tool")
    tool = PipelineWriteTool(store, lambda: 1)
    out = await tool.run(PipelineWriteArgs(**{
        "action": "create", "name": "开发+审查",
        "nodes": [
            {"title": "A", "prompt": "做 A"},
            {"title": "审查", "prompt": "审查", "after": [0]},
        ],
    }), None)
    assert "draft" in out and "等用户启动" in out

    pipes = await store.list_pipelines(1)
    assert len(pipes) == 1 and pipes[0]["nodes"][1]["depends_on"] == [pipes[0]["nodes"][0]["id"]]

    # 项目隔离：换一个项目 id 就看不到也改不了
    tool2 = PipelineWriteTool(store, lambda: 2)
    lst = await tool2.run(PipelineWriteArgs(action="list"), None)
    assert lst == "no pipelines"
    with pytest.raises(ToolError):
        await tool2.run(PipelineWriteArgs(action="get", id=pipes[0]["id"]), None)

    # update_node 造环要拒绝
    node_a = pipes[0]["nodes"][0]["id"]
    node_r = pipes[0]["nodes"][1]["id"]
    with pytest.raises(ToolError):
        await tool.run(PipelineWriteArgs(
            action="update_node", node_id=node_a, depends_on=[node_r]), None)

    # update_node 改成不存在的依赖要拒绝
    with pytest.raises(ToolError):
        await tool.run(PipelineWriteArgs(
            action="update_node", node_id=node_a, depends_on=[424242]), None)


async def test_pipeline_tool_clear_allowed_tools_and_caps(store):
    """update_node 能显式清空预授权名单；节点数/文本长度超限要拒绝。"""
    await store.get_or_create_project("/tmp/pl-caps")
    tool = PipelineWriteTool(store, lambda: 1)

    await tool.run(PipelineWriteArgs(**{
        "action": "create", "name": "授权",
        "nodes": [{"title": "写手", "prompt": "写文件",
                   "allowed_tools": ["write_file"]}],
    }), None)
    pipe = await store.list_pipelines(1)
    node_id = pipe[0]["nodes"][0]["id"]
    assert pipe[0]["nodes"][0]["allowed_tools"] == ["write_file"]

    # allowed_tools=[] 是「不改」（伴随其他字段一起给时不得误清空）；清空要走 clear_allowed_tools
    await tool.run(PipelineWriteArgs(
        action="update_node", node_id=node_id, title="写手二号", allowed_tools=[]), None)
    node = await store.get_pipeline_node(node_id)
    assert node["allowed_tools"] == ["write_file"] and node["title"] == "写手二号"
    await tool.run(PipelineWriteArgs(
        action="update_node", node_id=node_id, clear_allowed_tools=True), None)
    assert (await store.get_pipeline_node(node_id))["allowed_tools"] == []

    # 节点数上限（50）
    with pytest.raises(ValueError):
        await store.add_pipeline(1, "太多", nodes=[
            {"title": f"n{i}", "prompt": "x"} for i in range(51)
        ])
    # 单条指令长度上限（1 万字）
    with pytest.raises(ValueError):
        await store.add_pipeline(1, "太长", nodes=[
            {"title": "n", "prompt": "长" * 10_001}])
    # 改节点指令同样受上限约束
    with pytest.raises(ValueError):
        await store.update_pipeline_node(node_id, prompt="长" * 10_001)


def test_pipeline_start_without_runnable_nodes_errors(home):
    """失败且没有可重置节点的流水线：start 给明确指引，而不是静默再收尾一次。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([[], [TextBlock(text="这回有产出了")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "全停", "nodes": [
                {"title": "坏节点", "prompt": "干活"},
                {"title": "下游", "prompt": "接着做", "after": [0]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("failed",))

        ws.send_json({"id": "s2", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        frame = recv_until(ws, "s2")
        assert not frame["ok"], "失败流水线直接 start 应当报错指引重跑"
        assert "重跑" in frame["error"]
        assert done["status"] == "failed"


# ---- WS 端到端：草稿 → 启动 → 依赖释放 → 审查注入 ----


def _wait_pipe(ws, pipe_id, wanted, deadline=15.0):
    """轮询 pipeline.get 直到流水线进入目标状态（测试里比等事件稳）。"""
    from test_server import recv_until

    end = time.time() + deadline
    rid = 0
    last = None
    while time.time() < end:
        rid += 1
        ws.send_json({"id": f"w{rid}", "method": "pipeline.get", "params": {"id": pipe_id}})
        frame = recv_until(ws, f"w{rid}")  # 跳过流水线广播等事件帧，只认自己的响应
        if frame.get("ok"):
            p = frame["result"]["pipeline"]
            last = p
            if p["status"] in wanted:
                return p
        time.sleep(0.05)
    nodes = [(n["title"], n["status"], n["last_error"][:80]) for n in last["nodes"]] if last else None
    raise AssertionError(
        f"流水线 {pipe_id} 未在 {deadline}s 内进入 {wanted}；"
        f"最后状态={last['status'] if last else None} 节点={nodes}"
    )


def test_pipeline_end_to_end_dependency_and_injection(home):
    """并行两节点 + 汇总节点：汇总必须等两个都完成，且指令里带上游产出。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [TextBlock(text="功能A完成")],
        [TextBlock(text="功能B完成")],
        [TextBlock(text="审查：两边都好")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "登录模块", "concurrency": 2,
            "nodes": [
                {"title": "功能 A", "prompt": "做 A"},
                {"title": "功能 B", "prompt": "做 B"},
                {"title": "审查汇总", "prompt": "汇总审查", "after": [0, 1]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        assert pipe["status"] == "draft", "创建出来必须是草稿，等人启动"

        # 草稿不会被扫描循环偷偷执行
        time.sleep(0.3)
        ws.send_json({"id": "g0", "method": "pipeline.get", "params": {"id": pipe["id"]}})
        assert recv_until(ws, "g0")["result"]["pipeline"]["status"] == "draft"

        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done",))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["功能 A"]["status"] == "done"
        assert by_title["功能 B"]["status"] == "done"
        assert by_title["审查汇总"]["status"] == "done"
        assert by_title["审查汇总"]["result"] == "审查：两边都好"

        # 汇总节点的 prompt 注入了两个上游的产出（依赖释放的关键）
        injected = [m for m in provider.calls
                    if m and getattr(m[-1], "role", "") == "user"
                    and "前置任务" in m[-1].text]
        assert injected, "汇总节点应收到注入了上游产出的 prompt"
        blob = injected[-1][-1].text
        assert "功能A完成" in blob and "功能B完成" in blob


def test_pipeline_failure_blocks_downstream_and_rerun_releases(home):
    """节点没有产出 → 失败；依赖它的下游不自动运行（标「依赖失败」）；
    重跑上游成功后，下游被一并放行继续跑完。"""
    from test_server import make_client, recv_until

    # 第 1 组空脚本：坏节点没有产出 → error；下游不跑；重跑后依次消费后两组
    provider = FakeProvider([
        [],
        [TextBlock(text="这回有产出了")],
        [TextBlock(text="下游完成")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "会失败", "nodes": [
                {"title": "坏节点", "prompt": "干活"},
                {"title": "下游", "prompt": "接着做", "after": [0]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("failed",))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert done["status"] == "failed"
        assert by_title["坏节点"]["status"] == "error", "没有产出应计为失败"
        assert by_title["下游"]["status"] == "error"
        assert "依赖的节点" in by_title["下游"]["last_error"], "下游应标「依赖失败」而不是照跑"
        assert "运行中" not in [n["status"] for n in done["nodes"]]

        # 重跑失败节点：这次有产出 → done，下游被放行跑完 → 流水线 done
        bad_id = by_title["坏节点"]["id"]
        ws.send_json({"id": "rr", "method": "pipeline.node_rerun", "params": {"id": bad_id}})
        recv_until(ws, "rr")
        done2 = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title2 = {n["title"]: n for n in done2["nodes"]}
        assert done2["status"] == "done"
        assert by_title2["下游"]["result"] == "下游完成"
        assert "依赖的节点" not in by_title2["下游"]["last_error"]


def test_pipeline_headless_gate_authorized_write_only(home):
    """预授权的 write_file 落盘；未授权节点的写入被 headless 门控自动拒绝。"""
    from pathlib import Path

    from test_server import make_client, recv_until

    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "hello.txt", "content": "hi"})],
        [TextBlock(text="写好了")],
        [TextBlock(text="汇总完成")],
        [ToolUseBlock(id="w2", name="write_file",
                      input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="没写成也说句话")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "门控", "nodes": [
                {"title": "写手", "prompt": "写 hello.txt",
                 "allowed_tools": ["write_file"]},
                {"title": "汇总", "prompt": "汇总", "after": [0]},
                {"title": "偷写者", "prompt": "偷偷写 secret.txt", "after": [1]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert done["status"] == "done"
        assert by_title["写手"]["status"] == "done"
        assert by_title["汇总"]["result"] == "汇总完成"
        assert by_title["偷写者"]["status"] == "done", "写入被拒后 Agent 继续收尾"
        assert (Path(home) / "proj" / "hello.txt").read_text(encoding="utf-8") == "hi"
        assert not (Path(home) / "proj" / "secret.txt").exists(), "未授权写入必须被拒"


# ---- 纳入现有任务：store 列迁移 / 工具挂接与导入 ----


async def test_pipeline_node_kind_columns_and_legacy_migration(tmp_path):
    """旧库（没有 kind/ref_id 列）连接时自动补列，已有数据不受影响。"""
    import aiosqlite


    # 手工造一个「旧版」库：只建 pipelines + 旧列的 pipeline_nodes
    db = await aiosqlite.connect(tmp_path / "legacy.db")
    await db.executescript("""
        CREATE TABLE pipelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            concurrency INTEGER NOT NULL DEFAULT 2,
            created_at REAL NOT NULL,
            finished_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE pipeline_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id INTEGER NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            allowed_tools TEXT NOT NULL DEFAULT '',
            depends_on TEXT NOT NULL DEFAULT '',
            dep_mode TEXT NOT NULL DEFAULT 'all',
            status TEXT NOT NULL DEFAULT 'blocked',
            result TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            runs INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            finished_at REAL NOT NULL DEFAULT 0
        );
    """)
    await db.execute(
        "INSERT INTO pipelines (project_id, name, created_at) VALUES (1, '旧线', 0)"
    )
    await db.execute(
        "INSERT INTO pipeline_nodes (pipeline_id, seq, title, prompt) VALUES (1, 0, '旧节点', 'x')"
    )
    await db.commit()
    await db.close()

    from skysheep.session.store import SessionStore

    s = await SessionStore(tmp_path / "legacy.db").connect()
    try:
        pipe = await s.get_pipeline(1)
        node = pipe["nodes"][0]
        assert node["kind"] == "run" and node["ref_id"] == "", "旧数据补列后走默认值"
        # 新列可直接使用
        await s.add_pipeline_node(1, "挂接", "", kind="task", ref_id="abc123")
        nodes = await s.list_pipeline_nodes(1)
        assert nodes[1]["kind"] == "task" and nodes[1]["ref_id"] == "abc123"
        with pytest.raises(ValueError):
            await s.add_pipeline_node(1, "坏", "x", kind="bogus")
    finally:
        await s.close()


async def test_pipeline_tool_attach_and_import_cron(store):
    """工具层：add_node 挂接任务簿任务（校验存在性、跟随状态）、import_cron 复制定时任务。"""
    from types import SimpleNamespace

    await store.get_or_create_project("/tmp/pl-tool2")
    fake_rec = SimpleNamespace(status="running", result=None, error=None, prompt="调研登录模块")
    tool = PipelineWriteTool(store, lambda: 1, tasks_fn=lambda: SimpleNamespace(
        status=lambda tid: fake_rec if tid == "t1" else None,
    ))
    pipe = await store.add_pipeline(1, "挂接线", nodes=[])
    # 挂接存在的任务：不带 prompt 也行，标题缺省取任务指令
    out = await tool.run(PipelineWriteArgs(
        action="add_node", id=pipe["id"], task_id="t1"), None)
    assert "挂接任务" in out and "运行中" in out
    node = (await store.get_pipeline(pipe["id"]))["nodes"][0]
    assert node["kind"] == "task" and node["ref_id"] == "t1" and node["status"] == "running"

    # 挂接不存在的任务要拒绝
    with pytest.raises(ToolError):
        await tool.run(PipelineWriteArgs(
            action="add_node", id=pipe["id"], task_id="ghost"), None)

    # import_cron：复制指令与预授权；disable_source 停用原任务
    cron = await store.add_cron_task(1, "每日报表", "扫一遍 TODO", "interval",
                                     interval_minutes=30, allowed_tools=["read_file"])
    out = await tool.run(PipelineWriteArgs(
        action="import_cron", id=pipe["id"], cron_id=cron["id"], disable_source=True), None)
    assert "每日报表" in out and "已停用" in out
    nodes = (await store.get_pipeline(pipe["id"]))["nodes"]
    imported = nodes[1]
    assert imported["kind"] == "run" and imported["prompt"] == "扫一遍 TODO"
    assert imported["allowed_tools"] == ["read_file"]
    assert (await store.get_cron_task(cron["id"]))["enabled"] is False

    # 跨项目定时任务不能导入
    cron2 = await store.add_cron_task(2, "别家的", "x", "interval", interval_minutes=30)
    with pytest.raises(ToolError):
        await tool.run(PipelineWriteArgs(
            action="import_cron", id=pipe["id"], cron_id=cron2["id"]), None)


def test_pipeline_attach_running_background_task(home):
    """核心场景：正在跑的后台任务挂进流水线，下游审查节点等它完成并拿到产出。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [TextBlock(text="BG-REPORT: 登录模块调研完成")],  # 后台任务的一轮
        [TextBlock(text="审查：调研已收到")],  # 审查节点的一轮
    ])
    with (
        make_client(home, [], provider=provider) as client,
        client.websocket_connect("/ws") as ws,
    ):
        backend = client.app.state.backend
        # 直接在任务簿起一个后台任务（比经聊天派生少一层脚本时序耦合）
        # start_background 要在引擎事件循环里调（TestClient 的 portal 线程）
        task_id = client.portal.call(
            backend.tasks.start_background, "explore", "调研登录模块")

        # 建线即带挂接节点：审查节点依赖它（after=0），等任务完成才放行
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "挂接线", "nodes": [
                {"task_id": task_id},
                {"title": "审查汇总", "prompt": "汇总审查", "after": [0]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        assert pipe["nodes"][0]["kind"] == "task"
        assert pipe["nodes"][0]["ref_id"] == task_id
        assert pipe["nodes"][0]["status"] in ("running", "done"), "挂接即跟随任务当前状态"

        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert done["status"] == "done"
        attached = by_title[[n["title"] for n in done["nodes"] if n["kind"] == "task"][0]]
        assert attached["status"] == "done"
        assert "BG-REPORT" in attached["result"], "挂接节点的产出应来自原任务"
        review = by_title["审查汇总"]
        assert review["status"] == "done"
        injected = [m for m in provider.calls
                    if m and getattr(m[-1], "role", "") == "user"
                    and "BG-REPORT" in m[-1].text]
        assert injected, "审查节点应收到注入了挂接任务产出的 prompt"

        # 挂接节点不能单独重跑
        ws.send_json({"id": "rr", "method": "pipeline.node_rerun",
                      "params": {"id": attached["id"]}})
        r = recv_until(ws, "rr")
        assert not r["ok"] and "挂接" in r["error"]


def test_pipeline_attach_method_and_session_node(home):
    """pipeline.attach 追加挂接节点；pipeline.add_session 会话续跑带历史上下文。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [TextBlock(text="会话里的第一轮回答")],  # 会话已有历史：用户先聊过一轮
        [TextBlock(text="SESSION-CONT: 在原会话里续跑完成")],  # 会话节点的一轮
    ])
    with (
        make_client(home, [], provider=provider) as client,
        client.websocket_connect("/ws") as ws,
    ):
        # 先造一个有历史的会话：正常发一轮对话
        ws.send_json({"id": "chat", "method": "chat.send", "params": {
            "text": "帮我看看项目结构", "session_id": ""}})
        chat_done = recv_until(ws, "chat")
        assert chat_done["ok"], chat_done.get("error")
        sid = chat_done["result"]["session_id"]

        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "续跑线", "nodes": [
                {"title": "占位", "prompt": "先做点别的"},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]

        # attach：把（已完成的）占位后台任务挂进来——不存在时明确报错
        ws.send_json({"id": "at0", "method": "pipeline.attach", "params": {
            "id": pipe["id"], "task_id": "ghost"}})
        bad = recv_until(ws, "at0")
        assert not bad["ok"], "挂接不存在的任务要报错"

        # add_session：会话节点依赖占位节点
        ws.send_json({"id": "as", "method": "pipeline.add_session", "params": {
            "id": pipe["id"], "session_id": sid,
            "prompt": "结合刚才的结论继续深入", "depends_on": [pipe["nodes"][0]["id"]],}})
        r = recv_until(ws, "as")
        assert r["ok"], r.get("error")
        assert r["result"]["node"]["kind"] == "session"

        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert done["status"] == "done"
        sess_node = by_title[[n["title"] for n in done["nodes"] if n["kind"] == "session"][0]]
        assert sess_node["status"] == "done"
        assert sess_node["session_id"] == sid, "会话节点应落在原会话上"

        # 续跑那轮的 prompt 带上了原会话历史与上游注入（provider.calls 里找）
        cont = [m for m in provider.calls
                if m and getattr(m[-1], "role", "") == "user"
                and "结合刚才的结论" in m[-1].text]
        assert cont, "会话节点应发出了续跑指令"
        blob = "".join(getattr(m, "text", "") for m in cont[-1])
        assert "第一轮回答" in blob, "续跑应带原会话历史"
        assert "前置任务" in blob, "续跑应注入上游节点产出"


def test_pipeline_import_cron_ws(home):
    """WS 导入定时任务：复制指令与预授权成节点，可选停用原任务。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [TextBlock(text="占位完成")],
        [TextBlock(text="报表跑完了")],
    ])
    with (
        make_client(home, [], provider=provider) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "每日报表", "prompt": "扫一遍 TODO 汇总",
            "schedule_type": "interval", "interval_minutes": 30,
            "allowed_tools": ["read_file"],
        }})
        cron = recv_until(ws, "ca")["result"]
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "导入线", "nodes": [{"title": "占位", "prompt": "先做点别的"}],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "ic", "method": "pipeline.import_cron", "params": {
            "id": pipe["id"], "cron_id": cron["id"], "disable_source": True}})
        r = recv_until(ws, "ic")
        assert r["ok"], r.get("error")
        node = r["result"]["node"]
        assert node["prompt"] == "扫一遍 TODO 汇总"
        assert node["allowed_tools"] == ["read_file"]
        assert r["result"]["source_disabled"] is True

        ws.send_json({"id": "gl", "method": "cron.list", "params": {}})
        crons = recv_until(ws, "gl")["result"]["tasks"]
        assert crons[0]["enabled"] is False, "disable_source 应停用原定时任务"

        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        imported = next(n for n in done["nodes"] if n["title"].startswith("定时任务："))
        assert imported["status"] == "done" and imported["result"] == "报表跑完了"


# ---- 控制流：条件门 / 终止 / 迭代 / 失败重试 ----


def test_pipeline_control_gate_pass_and_fail(home):
    """条件门 PASS 放行下游；FAIL 把下游标 skipped（不算失败，流水线正常收尾）。"""
    from test_server import make_client, recv_until

    # 场景一：门输出 PASS → 下游照常运行
    provider = FakeProvider([[TextBlock(text="PASS")], [TextBlock(text="下游完成")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "门-通过", "nodes": [
                {"title": "检查", "prompt": "检查条件", "control": "gate"},
                {"title": "下游", "prompt": "接着做", "after": [0]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["检查"]["status"] == "done"
        assert by_title["下游"]["status"] == "done", "PASS 应放行下游"

    # 场景二：门输出 FAIL → 下游 skipped，流水线仍算正常结束
    provider = FakeProvider([[TextBlock(text="FAIL")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "门-拦截", "nodes": [
                {"title": "检查", "prompt": "检查条件", "control": "gate"},
                {"title": "下游", "prompt": "接着做", "after": [0]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["检查"]["status"] == "done", "门本身是正常完成"
        assert by_title["下游"]["status"] == "skipped", "FAIL 应把下游标跳过"
        assert "条件门" in by_title["下游"]["last_error"]
        assert done["status"] == "done", "skipped 是主动跳过，流水线不算失败"


def test_pipeline_control_stop_halts_pipeline(home):
    """终止节点依赖满足即截停：自身 done，未开始的节点全部取消，流水线置已停止。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([[TextBlock(text="上游完成")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "会终止", "nodes": [
                {"title": "上游", "prompt": "做上游"},
                {"title": "到此为止", "prompt": "", "control": "stop", "after": [0]},
                {"title": "不该跑", "prompt": "不会执行", "after": [1]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("cancelled", "done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert done["status"] == "cancelled"
        assert by_title["上游"]["status"] == "done"
        assert by_title["到此为止"]["status"] == "done"
        assert by_title["不该跑"]["status"] == "cancelled"


def test_pipeline_control_loop_until_done(home):
    """迭代节点：第一轮没有 DONE 标记 → 自动带着产出再来一轮；出现 DONE 才完成。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [TextBlock(text="第一轮：还有一半没改完")],
        [TextBlock(text="DONE\n全部打磨完成")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "会迭代", "nodes": [
                {"title": "打磨", "prompt": "反复打磨这段文案", "control": "loop", "max_runs": 3},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        node = done["nodes"][0]
        assert node["status"] == "done"
        assert node["runs"] == 2, "第一轮未 DONE 自动再来一轮"
        assert node["result"] == "全部打磨完成", "DONE 标记行应从产出里剥掉"
        # 第二轮的 prompt 带着第一轮产出（迭代不是从零重来）
        second = [m for m in provider.calls
                  if m and getattr(m[-1], "role", "") == "user" and "上一轮的产出" in m[-1].text]
        assert second and "还有一半没改完" in second[-1][-1].text


def test_pipeline_control_retry_on_empty_output(home):
    """失败自动重试：max_runs=2 的节点第一轮没有产出 → 自动再试一次而不是直接失败。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [],
        [TextBlock(text="第二次成功了")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "会重试", "nodes": [
                {"title": "易失败节点", "prompt": "干活", "max_runs": 2},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        node = done["nodes"][0]
        assert done["status"] == "done"
        assert node["status"] == "done" and node["runs"] == 2
        assert node["result"] == "第二次成功了"




# ---- 本次补强：any 语义 / 门无标记 / 超时 / busy 退避 / 重试上下文 / 级联重跑 / 复制导入导出 ----


class _SlowFake(FakeProvider):
    """带延迟的脚本假模型：给指定标记的轮次插入延迟，制造「A 已失败、B 还在跑」
    的确定时间窗——any 模式的依赖判定修复必须在这样的窗口里验证。"""

    def __init__(self, scripted, marker, delay):
        super().__init__(scripted)
        self.marker = marker
        self.delay = delay

    async def stream(self, messages, tool_schemas, effort=None):
        import asyncio

        last = messages[-1] if messages else None
        if last is not None and self.marker in (last.text or ""):
            await asyncio.sleep(self.delay)
        async for ev in super().stream(messages, tool_schemas, effort=effort):
            yield ev


def test_pipeline_any_mode_waits_instead_of_dying_on_one_failed_dep(home):
    """any 语义修复：上游 A 失败、B 还在跑时，下游不得提前判死；B 完成后照常运行。"""
    from test_server import make_client, recv_until

    provider = _SlowFake(
        [
            [],  # A：无产出 → 失败
            [TextBlock(text="B 完成")],
            [TextBlock(text="下游完成")],
        ],
        marker="做 B", delay=1.5,
    )
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "any-不等死", "concurrency": 2, "nodes": [
                {"title": "A", "prompt": "做 A"},
                {"title": "B", "prompt": "做 B"},
                {"title": "下游", "prompt": "汇总", "after": [0, 1], "dep_mode": "any"},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["A"]["status"] == "error"
        assert by_title["下游"]["status"] == "done", (
            "any 模式下 B 还在跑时下游不能因 A 失败被提前判死"
        )
        assert by_title["下游"]["result"] == "下游完成"
        # A 确实失败了：流水线整体仍报 failed（与 all 模式同一收尾规则），
        # 但关键是下游已经拿到结果、没有陪着陪葬
        assert done["status"] == "failed"


def test_pipeline_any_mode_all_deps_failed_marks_error(home):
    """any 模式全部依赖终态且无一完成：下游标失败（而不是永远等待）。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([[], []])  # A、B 都无产出
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "any-全挂", "concurrency": 2, "nodes": [
                {"title": "A", "prompt": "做 A"},
                {"title": "B", "prompt": "做 B"},
                {"title": "下游", "prompt": "汇总", "after": [0, 1], "dep_mode": "any"},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("failed",))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["下游"]["status"] == "error"
        assert "未成功" in by_title["下游"]["last_error"]


def test_pipeline_gate_unmarked_skips_with_clear_reason(home):
    """门节点没输出 PASS/FAIL 标记：下游跳过，且错误信息明确说「没标记」而非「FAIL」。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([[TextBlock(text="看起来没什么问题")]])  # 无标记
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "门-没标记", "nodes": [
                {"title": "检查", "prompt": "检查条件", "control": "gate"},
                {"title": "下游", "prompt": "接着做", "after": [0]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["检查"]["status"] == "done"
        assert by_title["下游"]["status"] == "skipped", "无标记按保守策略跳过下游"
        assert "PASS/FAIL 标记" in by_title["下游"]["last_error"], (
            "错误信息要区分「判 FAIL」与「忘了输出标记」"
        )
        assert "判定未通过" not in by_title["下游"]["last_error"]
        assert done["status"] == "done"


def test_pipeline_node_timeout_fails_and_retries(home, monkeypatch):
    """节点超时：限时到 → 按可重试失败处理；两轮都超时 → 标失败且说明超时。"""
    import asyncio

    from test_server import make_client, recv_until

    provider = FakeProvider([[TextBlock(text="永远轮不到我")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend

        async def slow_turn(*args, **kwargs):
            await asyncio.sleep(5)
            return {}

        monkeypatch.setattr(backend, "_run_turn_pipeline", slow_turn)
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "会超时", "nodes": [
                {"title": "慢节点", "prompt": "干不完的活", "max_runs": 2, "timeout_s": 1},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"), deadline=20.0)
        node = done["nodes"][0]
        assert done["status"] == "failed"
        assert node["status"] == "error"
        assert node["runs"] == 2, "超时按可重试失败处理，应自动重试过一次"
        assert "超时" in node["last_error"]


def test_pipeline_retry_prompt_carries_last_error(home):
    """自动重试的 prompt 应带上次失败原因，让 Agent 换思路而不是原样再挂。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [],
        [TextBlock(text="第二次成功了")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "重试带上下文", "nodes": [
                {"title": "易失败", "prompt": "干活", "max_runs": 2},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done",))
        assert done["status"] == "done"
        retry = [m for m in provider.calls
                 if m and getattr(m[-1], "role", "") == "user"
                 and "上一次失败原因" in m[-1].text]
        assert retry, "第二次尝试的 prompt 应注入上次失败原因"
        assert "节点没有产出" in retry[-1][-1].text


def test_pipeline_session_node_busy_backs_off_without_consuming_retry(home, monkeypatch):
    """会话续跑遇忙：不硬拒、不消耗重试次数，退避后自动重跑成功。"""
    from types import SimpleNamespace

    from test_server import make_client, recv_until

    provider = FakeProvider([
        [TextBlock(text="会话里的第一轮回答")],
        [TextBlock(text="SESSION-CONT: 忙完之后续跑成功")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        monkeypatch.setattr(backend, "BUSY_RETRY_WAIT_S", 1)  # 退避 1 秒，测试不干等

        ws.send_json({"id": "chat", "method": "chat.send", "params": {
            "text": "帮我看看项目结构", "session_id": ""}})
        chat_done = recv_until(ws, "chat")
        sid = chat_done["result"]["session_id"]

        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "忙退避", "nodes": [
                {"title": "续跑", "prompt": "继续深入", "session_id": sid},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]

        # 让会话看起来「正被使用」：塞一个 run_task 未完成的假 runtime
        always_busy = SimpleNamespace(run_task=SimpleNamespace(done=lambda: False))
        backend.runtimes[sid] = always_busy

        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")

        # 等第一轮预检命中 busy：节点保持 ready，错误信息说明退避而非失败
        end = time.time() + 10
        node = None
        rid = 0
        while time.time() < end:
            rid += 1
            ws.send_json({"id": f"g{rid}", "method": "pipeline.get",
                          "params": {"id": pipe["id"]}})
            frame = recv_until(ws, f"g{rid}")
            node = frame["result"]["pipeline"]["nodes"][0]
            if "会话正被使用" in (node["last_error"] or ""):
                break
            time.sleep(0.1)
        assert node is not None and "会话正被使用" in (node["last_error"] or ""), (
            "忙时应给出退避说明而不是失败"
        )
        assert node["status"] == "ready" and node["runs"] == 0, "忙等待不消耗尝试次数"

        # 会话空闲 → 退避到期后自动重跑成功
        backend.runtimes[sid] = SimpleNamespace(run_task=SimpleNamespace(done=lambda: True))
        done = _wait_pipe(ws, pipe["id"], ("done", "failed"), deadline=20.0)
        node = done["nodes"][0]
        assert done["status"] == "done"
        assert node["status"] == "done" and node["runs"] == 1, "重跑只应实际执行一次"
        assert "续跑成功" in node["result"]


def test_pipeline_rerun_done_node_cascades_downstream(home):
    """重跑已完成节点：先返回 needs_confirm 与下游清单；cascade=true 把下游一并重置。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([
        [TextBlock(text="A 完成")],
        [TextBlock(text="B 完成")],
        [TextBlock(text="C 完成")],
        [TextBlock(text="A 第二版")],
        [TextBlock(text="B 第二版")],
        [TextBlock(text="C 第二版")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "级联线", "nodes": [
                {"title": "A", "prompt": "做 A"},
                {"title": "B", "prompt": "做 B", "after": [0]},
                {"title": "C", "prompt": "做 C", "after": [1]},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]
        ws.send_json({"id": "s1", "method": "pipeline.start", "params": {"id": pipe["id"]}})
        recv_until(ws, "s1")
        done = _wait_pipe(ws, pipe["id"], ("done",))
        a_id = done["nodes"][0]["id"]

        # 不带 cascade：只确认，不动状态
        ws.send_json({"id": "rr0", "method": "pipeline.node_rerun", "params": {"id": a_id}})
        r0 = recv_until(ws, "rr0")["result"]
        assert r0.get("needs_confirm") is True
        assert r0["downstream"] == ["B", "C"], "应列出全部传递下游"
        still = _wait_pipe(ws, pipe["id"], ("done",))
        assert [n["status"] for n in still["nodes"]] == ["done", "done", "done"], (
            "确认前不能动任何节点"
        )

        # cascade=true：A/B/C 全部退回重跑并跑完
        ws.send_json({"id": "rr1", "method": "pipeline.node_rerun",
                      "params": {"id": a_id, "cascade": True}})
        recv_until(ws, "rr1")
        done2 = _wait_pipe(ws, pipe["id"], ("done", "failed"))
        by_title = {n["title"]: n for n in done2["nodes"]}
        assert done2["status"] == "done"
        assert by_title["A"]["result"] == "A 第二版"
        assert by_title["C"]["result"] == "C 第二版"
        assert [n["runs"] for n in done2["nodes"]] == [2, 2, 2]


def test_pipeline_duplicate_export_import_roundtrip(home):
    """复制为草稿、导出 JSON、再导入：结构与预授权一致，依赖关系重建，状态清零。"""
    from test_server import make_client, recv_until

    provider = FakeProvider([])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "pipeline.create", "params": {
            "name": "原始线", "concurrency": 3, "nodes": [
                {"title": "A", "prompt": "做 A", "allowed_tools": ["write_file"],
                 "timeout_s": 120},
                {"title": "B", "prompt": "做 B", "after": [0], "dep_mode": "any"},
            ],
        }})
        pipe = recv_until(ws, "c1")["result"]["pipeline"]

        # 复制：草稿态、结构与超时保持、状态清零
        ws.send_json({"id": "d1", "method": "pipeline.duplicate", "params": {"id": pipe["id"]}})
        dup = recv_until(ws, "d1")["result"]["pipeline"]
        assert dup["name"] == "原始线（副本）" and dup["status"] == "draft"
        assert dup["concurrency"] == 3
        assert [n["status"] for n in dup["nodes"]] == ["blocked", "blocked"]
        assert dup["nodes"][0]["timeout_s"] == 120
        assert dup["nodes"][0]["allowed_tools"] == ["write_file"]
        assert dup["nodes"][1]["depends_on"] == [dup["nodes"][0]["id"]], "依赖应映射到新节点 id"
        assert dup["id"] != pipe["id"]

        # 导出：依赖转同批次序号
        ws.send_json({"id": "e1", "method": "pipeline.export", "params": {"id": pipe["id"]}})
        exp = recv_until(ws, "e1")["result"]["export"]
        assert exp["format"] == "skysheep-pipeline" and exp["name"] == "原始线"
        assert exp["nodes"][1]["after"] == [0] and exp["nodes"][1]["dep_mode"] == "any"
        assert exp["nodes"][0]["timeout_s"] == 120

        # 导入：新流水线重建，依赖再次物化
        ws.send_json({"id": "i1", "method": "pipeline.import", "params": {"export": exp}})
        imp = recv_until(ws, "i1")["result"]["pipeline"]
        assert imp["id"] not in (pipe["id"], dup["id"])
        assert imp["status"] == "draft" and len(imp["nodes"]) == 2
        assert imp["nodes"][0]["prompt"] == "做 A"
        assert imp["nodes"][1]["depends_on"] == [imp["nodes"][0]["id"]]

        # 导入坏格式：明确报错
        ws.send_json({"id": "i2", "method": "pipeline.import",
                      "params": {"export": {"format": "other"}}})
        bad = recv_until(ws, "i2")
        assert not bad["ok"] and "format" in bad["error"]


async def test_pipeline_usage_aggregation(store):
    """流水线用量汇总：按节点 session 聚合 usage_log。"""
    project = await store.get_or_create_project("/tmp/pl-usage")
    pipe = await store.add_pipeline(project.id, "用量线", nodes=[
        {"title": "A", "prompt": "a"},
        {"title": "B", "prompt": "b", "depends_on": [0]},
    ])
    a, b = pipe["nodes"]
    await store.update_pipeline_node(a["id"], session_id="sess-a")
    await store.update_pipeline_node(b["id"], session_id="sess-b")
    await store.add_usage("sess-a", "fake", "fake-1", 100, 20)
    await store.add_usage("sess-b", "fake", "fake-1", 50, 10)
    await store.add_usage("sess-other", "fake", "fake-1", 999, 999)  # 无关节点不计入

    u = await store.pipeline_usage(pipe["id"])
    assert u["in_tokens"] == 150 and u["out_tokens"] == 30
