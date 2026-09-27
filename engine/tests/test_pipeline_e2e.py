"""真实 socket 上的流水线无人值守端到端测试。

test_pipeline.py 的 WS 用例全部走 ``fastapi.testclient.TestClient``——进程内
ASGI 直调，不经过真实 socket；这里补上真实网络栈这一层：uvicorn 监听随机
端口 + websockets 客户端真连，验证任务编排的无人值守执行链路：

- 含会话节点的流水线经真实分帧创建、启动，依赖释放后在无人参与的情况下
  跑到完成；会话节点续跑在原会话上，prompt 带原会话历史与上游产出注入；
- 全程前端 socket 上不得出现 permission_request——无人值守节点由
  HeadlessGate 自动放行/拒绝，绝不阻塞等人（headless 写入被拒后 Agent
  继续收尾，落盘不得发生）。

用 FakeProvider 脚本回放，不需要网络与任何外部凭据；标记为 e2e，用
``pytest -m e2e`` 单独跑、``pytest -m "not e2e"`` 跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from pathlib import Path

import pytest
import websockets
from test_ws_e2e import _rpc

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    """让操作系统分配一个空闲端口，避免并行跑测试时撞端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home, provider):
    """起一个真实 uvicorn 服务（随机端口），注入现成的 FakeProvider 实例，
    yield 出 ws:// 地址——测试要断言 provider.calls（脚本被谁消费、prompt 注入）。"""
    import uvicorn

    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        # 等真正开始监听：轮询到端口可连为止
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("uvicorn 未能在超时内启动")
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


async def _wait_pipe(ws, pipe_id, wanted, deadline=30.0, seen_events=None):
    """真实 socket 上轮询 pipeline.get 直到流水线进入目标状态。

    途中的事件帧（流水线广播等）跳过并记录到 seen_events，供调用方断言
    「无人值守全程没有权限请求弹给人」。
    """
    end = time.monotonic() + deadline
    rid = 0
    last = None
    while time.monotonic() < end:
        rid += 1
        frame = await _rpc(ws, f"w{rid}", "pipeline.get", {"id": pipe_id},
                           events=seen_events)
        assert frame.get("ok"), frame
        last = frame["result"]["pipeline"]
        if last["status"] in wanted:
            return last
        await asyncio.sleep(0.05)
    nodes = [(n["title"], n["status"], (n["last_error"] or "")[:80])
             for n in last["nodes"]] if last else None
    raise AssertionError(
        f"流水线 {pipe_id} 未在 {deadline}s 内进入 {wanted}；"
        f"最后状态={last['status'] if last else None} 节点={nodes}"
    )


async def test_real_socket_pipeline_with_session_node_runs_unattended(home):
    """真实 socket 建一条含会话节点的流水线并跑到完成：依赖释放、原会话续跑、
    上游产出注入，全程无人参与（socket 上没有权限请求）。"""
    provider = FakeProvider([
        [TextBlock(text="会话里的第一轮回答")],  # 会话已有历史：用户先聊过一轮
        [TextBlock(text="占位完成")],  # run 节点的一轮
        [TextBlock(text="SESSION-CONT: 在原会话里续跑完成")],  # 会话节点的一轮
    ])
    seen: list = []
    async with live_server(home, provider) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot

            # 先经真实 socket 聊一轮，造出有历史的会话
            ev: list = []
            chat = await _rpc(ws, "chat", "chat.send",
                              {"text": "帮我看看项目结构", "session_id": ""},
                              events=ev)
            assert chat.get("ok") is True, chat
            sid = chat["result"]["session_id"]

            # 建线：一个 run 节点；再挂会话节点依赖它
            created = await _rpc(ws, "c1", "pipeline.create", {
                "name": "真socket续跑线",
                "nodes": [{"title": "占位", "prompt": "先做点别的"}],
            })
            assert created.get("ok") is True, created
            pipe = created["result"]["pipeline"]
            assert pipe["status"] == "draft", "创建出来必须是草稿，等人启动"

            added = await _rpc(ws, "as", "pipeline.add_session", {
                "id": pipe["id"], "session_id": sid,
                "prompt": "结合刚才的结论继续深入",
                "depends_on": [pipe["nodes"][0]["id"]],
            })
            assert added.get("ok") is True, added
            assert added["result"]["node"]["kind"] == "session"

            # 启动后不再发任何指令：依赖释放、执行、收尾必须全自动完成
            started = await _rpc(ws, "s1", "pipeline.start", {"id": pipe["id"]})
            assert started.get("ok") is True, started
            done = await _wait_pipe(ws, pipe["id"], ("done", "failed"),
                                    seen_events=seen)
            assert done["status"] == "done", done

            by_kind = {n["kind"]: n for n in done["nodes"]}
            run_node = by_kind["run"]
            sess_node = by_kind["session"]
            assert run_node["status"] == "done"
            assert run_node["result"] == "占位完成"
            assert sess_node["status"] == "done"
            assert sess_node["session_id"] == sid, "会话节点应落在原会话上"
            assert sess_node["result"] == "SESSION-CONT: 在原会话里续跑完成"

            # 会话节点续跑那轮：带原会话历史 + 注入上游产出
            cont = [m for m in provider.calls
                    if m and getattr(m[-1], "role", "") == "user"
                    and "结合刚才的结论" in m[-1].text]
            assert cont, "会话节点应发出了续跑指令"
            blob = "".join(getattr(m, "text", "") for m in cont[-1])
            assert "第一轮回答" in blob, "续跑应带原会话历史"
            assert "前置任务" in blob and "占位完成" in blob, "续跑应注入上游节点产出"

            # 无人值守的关键：整条流水线执行期间，前端 socket 上没有权限请求
            kinds = [e.get("event") for e in seen]
            assert "permission_request" not in kinds, kinds


async def test_real_socket_pipeline_unattended_gate_denies_without_prompt(home):
    """未预授权的写入在无人值守节点里被自动拒绝：不弹权限请求、不落盘，
    Agent 收到拒绝后继续收尾，流水线正常完成。"""
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="没写成也说句话")],
    ])
    seen: list = []
    async with live_server(home, provider) as url:
        async with websockets.connect(url) as ws:
            await _rpc(ws, "b1", "boot")

            created = await _rpc(ws, "c1", "pipeline.create", {
                "name": "真socket门控线",
                "nodes": [{"title": "偷写者", "prompt": "偷偷写 secret.txt"}],
            })
            assert created.get("ok") is True, created
            pipe = created["result"]["pipeline"]

            started = await _rpc(ws, "s1", "pipeline.start", {"id": pipe["id"]})
            assert started.get("ok") is True, started
            done = await _wait_pipe(ws, pipe["id"], ("done", "failed"),
                                    seen_events=seen)
            assert done["status"] == "done", done
            node = done["nodes"][0]
            assert node["status"] == "done", node
            assert node["result"] == "没写成也说句话", "写入被拒后 Agent 继续收尾"

            assert not (Path(home) / "proj" / "secret.txt").exists(), (
                "未授权写入必须被拒，不得落盘"
            )
            kinds = [e.get("event") for e in seen]
            assert "permission_request" not in kinds, (
                "无人值守执行不得往前端发权限请求"
            )
