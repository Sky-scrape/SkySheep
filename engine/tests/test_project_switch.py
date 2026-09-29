"""工作项目切换测试：应用内设定工作目录（project.switch）。"""

from __future__ import annotations

import asyncio

import pytest
from test_server import make_client, recv_until  # helpers（home fixture 在 conftest.py）

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.base import ProviderDone, ProviderTextDelta
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend


class SlowStreamProvider:
    """每轮输出前先睡一会儿，制造「运行中 + 排队」窗口。"""

    model = "slow-1"

    async def stream(self, messages, tools, effort=None):
        yield ProviderTextDelta("处理中…")
        await asyncio.sleep(0.25)
        yield ProviderDone(input_tokens=5, output_tokens=5)


def test_switch_project_moves_engine_to_new_dir(home):
    # 新项目目录：带自己的 AGENTS.md 和一个文件
    proj2 = home / "proj2"
    (proj2 / "src").mkdir(parents=True)
    (proj2 / "AGENTS.md").write_text("proj2 的约定：全部用中文回复", encoding="utf-8")
    (proj2 / "src" / "app.py").write_text("print('proj2')", encoding="utf-8")
    # 旧项目留一个文件，切换后不应出现在 fs.files
    (home / "proj" / "old.txt").write_text("old", encoding="utf-8")

    provider = FakeProvider([])
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        # 切换
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        r = recv_until(ws, "sw")
        assert r["ok"] and r["result"]["switched"]
        assert r["result"]["path"] == str(proj2.resolve())

        # 快照反映新项目
        ws.send_json({"id": "b", "method": "boot", "params": {}})
        snap = recv_until(ws, "b")["result"]
        assert snap["working_dir"] == str(proj2.resolve())
        assert snap["instructions_file"].endswith("AGENTS.md")

        # 文件索引只见新项目
        ws.send_json({"id": "f", "method": "fs.files", "params": {}})
        files = recv_until(ws, "f")["result"]
        assert "src/app.py" in files["files"]
        assert not any(p.endswith("old.txt") for p in files["files"])

        # 项目列表：两个项目，当前是 proj2
        ws.send_json({"id": "pl", "method": "project.list", "params": {}})
        listing = recv_until(ws, "pl")["result"]
        current = [p for p in listing["projects"] if p["is_current"]]
        assert len(current) == 1 and current[0]["root_path"] == str(proj2.resolve())

        # 再次切换到同目录 → 幂等 no-op
        ws.send_json({"id": "sw2", "method": "project.switch", "params": {"path": str(proj2)}})
        r2 = recv_until(ws, "sw2")
        assert r2["ok"] and not r2["result"]["switched"]


def test_switch_project_then_tools_write_into_new_dir(home):
    proj2 = home / "proj2"
    proj2.mkdir()
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "made.txt", "content": "hi"})],
            [TextBlock(text="done")],
        ]
    )
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw")["ok"]

        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        while True:
            fr = ws.receive_json()
            if "event" in fr:
                if fr["event"] == "permission_request":
                    ws.send_json(
                        {
                            "id": "pa",
                            "method": "permission.respond",
                            "params": {"request_id": fr["data"]["request_id"], "decision": "allow_once"},
                        }
                    )
                continue
            if fr.get("id") == "c1":
                break
        assert fr["ok"]
        assert (proj2 / "made.txt").read_text(encoding="utf-8") == "hi"
        assert not (home / "proj" / "made.txt").exists()


def test_switch_project_rejects_bad_path_and_running_task(home):
    proj2 = home / "proj2"
    proj2.mkdir()
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "b.txt", "content": "x"})],
            [TextBlock(text="done")],
        ]
    )
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        # 不存在的目录
        ws.send_json(
            {"id": "bad", "method": "project.switch", "params": {"path": str(home / "nope")}}
        )
        r = recv_until(ws, "bad")
        assert not r["ok"] and "目录不存在" in r["error"]

        # 相对路径
        ws.send_json({"id": "rel", "method": "project.switch", "params": {"path": "proj2"}})
        assert not recv_until(ws, "rel")["ok"]

        # 任务运行中（权限挂起 = 未结束）拒绝切换
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        perm = None
        for _ in range(200):
            fr = ws.receive_json()
            if "event" in fr and fr["event"] == "permission_request":
                perm = fr["data"]["request_id"]
                break
        assert perm
        ws.send_json({"id": "busy", "method": "project.switch", "params": {"path": str(proj2)}})
        rb = recv_until(ws, "busy")
        assert not rb["ok"] and "正在运行" in rb["error"]

        # 放行让任务收尾，再切换应该成功
        ws.send_json(
            {
                "id": "pa",
                "method": "permission.respond",
                "params": {"request_id": perm, "decision": "allow_once"},
            }
        )
        done = recv_until(ws, "c1")
        assert done["ok"]
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw")["ok"]


async def test_switch_project_fails_queued_turns_readably(home):
    """切项目时后台会话的排队轮被逐个落空：拿到可读错误，runtime 全部释放。

    守卫只看活动会话（这是既有设计），后台会话有运行中/排队轮时照样进入
    _bind_project——排队轮的 Future 必须显式落空（与 delete_session 同一口径），
    否则要么等交棒轮在旧上下文里空跑一轮后拿到裸内部错误，要么在取消落在
    pipeline try 之前的窄竞态里永远挂死。
    """
    proj2 = home / "proj2-drain"
    proj2.mkdir()
    be = ServerBackend(working_dir=home / "proj", provider_factory=SlowStreamProvider)
    await be.setup()

    async def noop(ev):
        pass

    sid_a = (await be.new_session())["id"]
    sid_b = (await be.new_session())["id"]  # 后台会话：跑一轮 + 排一轮
    t_run = asyncio.create_task(be.send("后台轮", noop, session_id=sid_b))
    for _ in range(200):
        rt = be.runtimes.get(sid_b)
        if rt and rt.run_task:
            break
        await asyncio.sleep(0.01)
    t_queued = asyncio.create_task(be.send("排队轮", noop, session_id=sid_b))
    for _ in range(200):
        if be.runtimes[sid_b].queue:
            break
        await asyncio.sleep(0.01)
    assert be.runtimes[sid_b].queue, "前置条件：排队轮已在队列里"
    # 用户此刻点回会话 A 的标签：B 变成后台会话（守卫只看活动会话）
    await be.activate_session(sid_a)

    r = await be.switch_project(str(proj2))  # 守卫只看活动会话 → 放行
    assert r["switched"]

    with pytest.raises(RuntimeError, match="项目已切换"):
        await t_queued
    r_run = await t_run
    assert r_run["stopped"] is True
    assert not be.runtimes, "旧项目 runtime 应全部清空"
    assert not be._base_queue, "基底队列应清空"
    await be.shutdown()
