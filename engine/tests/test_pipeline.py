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
