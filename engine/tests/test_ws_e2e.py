"""真实网络栈的 WebSocket 端到端测试。

现有协议测试全部走 ``fastapi.testclient.TestClient``——那是进程内 ASGI 直调，
不经过真实 socket、握手与分帧。所以「真实网络栈」这一层此前完全没有覆盖：
协议帧的序列化/反序列化、握手校验、长连接下的多次往返，都只能靠手动验证。

这个文件用 uvicorn 监听随机端口 + websockets 客户端真连一次，跑通
boot → chat.send → 事件流 → 完成 的完整链路，覆盖：

- 真实握手通过（脚本客户端不带 Origin，应被放行）；
- boot 快照能从真实 socket 拿到；
- chat.send 的流式事件经真实分帧到达，且最终 assistant 文本拼得回来；
- 连续多轮复用同一条连接（长连接不因一轮结束而失效）。

标记为 e2e：依赖本机端口与事件循环调度，比进程内测试慢，用
``pytest -m e2e`` 单独跑、``pytest -m "not e2e"`` 跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sqlite3

import pytest
import websockets

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    """让操作系统分配一个空闲端口，避免并行跑测试时撞端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home, script):
    """起一个真实 uvicorn 服务（随机端口），yield 出 ws:// 地址。"""
    import uvicorn

    provider = FakeProvider(script)
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


async def _rpc(ws, mid: str, method: str, params: dict | None = None,
               events: list | None = None):
    """发一条 RPC 并读到它的回复；途中的事件帧收进 events。"""
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if frame.get("id") == mid:
            return frame


async def test_real_socket_boot_and_streaming_chat(home):
    """真实 socket 上跑通 boot → 一轮流式对话。"""
    script = [[TextBlock(text="你好，世界")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot
            assert "session" in (boot.get("result") or {})

            events: list = []
            reply = await _rpc(
                ws, "c1", "chat.send", {"text": "打个招呼"}, events=events
            )
            assert reply.get("ok") is True, reply

            # 流式正文经真实分帧到达，拼回来必须完整
            deltas = [
                e["data"].get("text", "")
                for e in events
                if e.get("event") == "text_delta"
            ]
            assert "".join(deltas) == "你好，世界", deltas
            # 轮次收尾事件一定到达（否则前端会一直显示「运行中」）
            kinds = [e.get("event") for e in events]
            assert "turn_finished" in kinds, kinds


async def test_real_socket_connection_survives_multiple_turns(home):
    """同一条真实连接连续跑两轮：长连接不该在一轮结束后失效。"""
    script = [[TextBlock(text="第一轮")], [TextBlock(text="第二轮")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws:
            await _rpc(ws, "b1", "boot")

            ev1: list = []
            r1 = await _rpc(ws, "c1", "chat.send", {"text": "一"}, events=ev1)
            assert r1.get("ok") is True, r1
            text1 = "".join(
                e["data"].get("text", "") for e in ev1 if e.get("event") == "text_delta"
            )
            assert text1 == "第一轮", text1

            ev2: list = []
            r2 = await _rpc(ws, "c2", "chat.send", {"text": "二"}, events=ev2)
            assert r2.get("ok") is True, r2
            text2 = "".join(
                e["data"].get("text", "") for e in ev2 if e.get("event") == "text_delta"
            )
            assert text2 == "第二轮", text2


async def test_real_socket_reports_error_for_unknown_method(home):
    """未知方法经真实 socket 返回 ok=false，而不是断开连接。"""
    async with live_server(home, []) as url:
        async with websockets.connect(url) as ws:
            reply = await _rpc(ws, "x1", "definitely.not.a.method")
            assert reply.get("ok") is False
            assert reply.get("error")
            # 连接仍可用
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True


async def test_real_socket_permission_round_trip(home):
    """真实 socket 上跑通「权限请求 → respond → 工具执行 → 轮次完成」的完整往返。

    进程内同款流程在 test_server.py 有覆盖；这里是真实网络栈版——PermissionRequest
    事件与 respond 回包都要经过真实的分帧与排队，任何一处顺序假设写错都会在这里挂。
    """
    from skysheep.messages import ToolUseBlock

    script = [
        [ToolUseBlock(id="t1", name="write_file", input={"path": "out.txt", "content": "hi"})],
        [TextBlock(text="文件已写入")],
    ]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws:
            await _rpc(ws, "b1", "boot")

            await ws.send(json.dumps({"id": "c1", "method": "chat.send", "params": {"text": "写个文件"}}))
            request_id = None
            resolved = None
            events: list = []
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "event" not in frame:
                    if frame.get("id") == "c1":
                        final = frame
                        break
                    continue
                events.append(frame)
                if frame["event"] == "permission_request" and request_id is None:
                    request_id = frame["data"]["request_id"]
                    await ws.send(json.dumps({
                        "id": "p1",
                        "method": "permission.respond",
                        "params": {"request_id": request_id, "decision": "allow_once"},
                    }))
                elif frame["event"] == "permission_resolved":
                    resolved = frame["data"]

            assert request_id, "真实 socket 上应收到权限请求事件"
            assert final.get("ok") is True, final
            assert resolved and resolved.get("decision") == "allow_once", events
            assert (home / "proj" / "out.txt").read_text(encoding="utf-8") == "hi"


async def test_real_socket_rejects_malformed_params_gracefully(home):
    """畸形参数经真实 socket 不炸服务：缺必填参数报 ok=false；错型被 str() 粗 coercion
    （app.py 的既有语义，int 会被转成字符串当消息发）。两条路径都不得断开连接。"""
    script = [[TextBlock(text="收到")], [TextBlock(text="还在")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws:
            await _rpc(ws, "b1", "boot")
            # chat.send 缺 text → 报错
            r1 = await _rpc(ws, "m1", "chat.send", {})
            assert r1.get("ok") is False, r1
            # 错型参数 → str() coercion 吞掉，不报错也不炸
            r2 = await _rpc(ws, "m2", "chat.send", {"text": 12345})
            assert r2.get("ok") is True, r2
            # 连接仍可用：正常一轮照常工作
            ev: list = []
            r3 = await _rpc(ws, "m3", "chat.send", {"text": "还在吗"}, events=ev)
            assert r3.get("ok") is True, r3
            assert "".join(
                e["data"].get("text", "") for e in ev if e.get("event") == "text_delta"
            ) == "还在"


async def test_real_socket_team_flow_create_assign_answer_finish(home):
    """真实 socket 上跑通团队一期（用户总管）的最小闭环：
    建队 → 派工 → 成员应答 → 轮次耗尽强制交付 → 会话恢复普通回合。

    max_rounds=1（config.toml 预置）：成员第二次应答的预算已见底，交付在
    同一轮收口——事件帧的合并、seq 排序与终态落库任何一处顺序假设写错都会
    在真实分帧上挂。"""
    (home / "home").mkdir(parents=True, exist_ok=True)
    (home / "home" / "config.toml").write_text(
        "[team]\nmax_rounds = 1\n", encoding="utf-8"
    )
    script = [
        [TextBlock(text="第一次应答：工单做完了")],  # 成员被唤醒的一轮
        [TextBlock(text="收队后的普通回答")],  # 交付后恢复的普通回合
    ]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot

            # ---- 建队：广播消息进频道（不唤醒成员）----
            ev1: list = []
            r1 = await _rpc(ws, "t1", "chat.send", {
                "text": "大家好，目标是整理调研结论", "team": True,
                "members": [{"provider": "fa", "model": "ma", "name": "小研",
                             "persona": "研究员"}],
            }, events=ev1)
            assert r1.get("ok") is True, r1
            started = next(e for e in ev1 if e["event"] == "team_started")
            assert started["data"]["director_mode"] == "user"
            assert started["data"]["roster"][0]["name"] == "小研"
            assert r1["result"]["team"]["mode"] == "team"
            sid = r1["result"]["session_id"]

            # ---- 派工：登记工单并置进行中（代发 director 的 assign 消息）----
            k1 = await _rpc(ws, "k1", "team.task_add", {
                "session_id": sid, "title": "整理调研结论",
                "assignee": "小研", "type": "exec",
            })
            assert k1.get("ok") is True, k1
            tid = k1["result"]["task"]["id"]
            ev2: list = []
            k2 = await _rpc(ws, "k2", "team.task_update", {
                "session_id": sid, "task_id": tid, "status": "in_progress",
            }, events=ev2)
            assert k2.get("ok") is True, k2
            assign = k2["result"]["assign_message"]
            assert assign["from"] == "director" and assign["msg_kind"] == "assign"
            assert any(e["event"] == "team_task_updated"
                       and e["data"]["status"] == "in_progress" for e in ev2)

            # ---- 成员应答 + 轮次耗尽强制交付（同一轮收口）----
            ev3: list = []
            r2 = await _rpc(ws, "t2", "chat.send", {
                "text": "@小研 干活", "session_id": sid,
            }, events=ev3)
            assert r2.get("ok") is True, r2
            # 消息没带 team 标志也自动路由进团队（会话级活动团队）
            assert r2["result"]["team"]["mode"] == "team"
            assert r2["result"]["team"]["team"]["status"] == "rounds_exhausted"
            assert r2["result"]["team"]["team"]["rounds_used"] == 1
            member_report = next(e for e in ev3 if e["event"] == "team_message"
                                 and e["data"]["from_member"] == "小研")
            assert member_report["data"]["to_member"] == "director"
            assert member_report["data"]["msg_kind"] == "report"
            deltas = "".join(e["data"]["text"] for e in ev3
                             if e["event"] == "team_message_delta")
            assert "第一次应答" in deltas
            finished = next(e for e in ev3 if e["event"] == "team_finished")
            assert finished["data"]["status"] == "rounds_exhausted"
            assert "未尽事项" in finished["data"]["summary"]

            # ---- 交付后恢复普通回合：消息不再进团队频道 ----
            ev4: list = []
            r3 = await _rpc(ws, "t3", "chat.send", {"text": "谢谢大家"}, events=ev4)
            assert r3.get("ok") is True, r3
            assert r3["result"]["team"] is None
            assert not any(e["event"].startswith("team_") for e in ev4)
            assert "".join(
                e["data"].get("text", "") for e in ev4 if e.get("event") == "text_delta"
            ) == "收队后的普通回答"
            g1 = await _rpc(ws, "g1", "team.get", {"session_id": sid})
            assert g1["result"]["active"] is False and g1["result"]["team"] is None


async def test_real_socket_ai_director_closed_loop(home):
    """真实 socket 上跑通团队二期（AI 总管）的闭环：
    一句话目标 → 总管建单 → 自动派发唤醒队员 → 验收 → 交付 → 会话恢复普通回合。

    总管与队员是独立的 provider 实例（factory 顺序：主模型 → 总管 → 队员）；
    TeamStarted 总管字段、member_index=-1 的总管增量、零权限事件、快照
    director 块与 usage_log 总管行任何一处接线错误都会在真实分帧上挂。"""
    import uvicorn

    from skysheep.messages import ToolUseBlock

    (home / "home").mkdir(parents=True, exist_ok=True)
    (home / "home" / "config.toml").write_text("[team]\nmax_rounds = 6\n", encoding="utf-8")
    pool = [
        FakeProvider([[TextBlock(text="收队后的普通回答")]]),  # 主模型（交付后恢复普通回合）
        FakeProvider([  # AI 总管：拆解建单 → 验收 → 交付
            [TextBlock(text="分工方案：先调研再交付"), ToolUseBlock(
                id="d1", name="team_assign",
                input={"title": "写调研报告", "assignee": "小研", "type": "exec",
                       "accept": "有结论"})],
            [TextBlock(text="建单完成，等队员报告")],
            [TextBlock(text="对照验收标准：合格"), ToolUseBlock(
                id="d2", name="team_accept", input={"task_id": "T1", "note": "结论清晰"})],
            [TextBlock(text="验收通过，全部工单完成")],
            [TextBlock(text="《交付说明》：调研报告已完成。"), ToolUseBlock(
                id="d3", name="team_deliver", input={})],
            [TextBlock(text="交付收口")],
        ]),
        FakeProvider([[TextBlock(text="调研完成：结论 A")]]),  # 队员小研
    ]
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool else FakeProvider([]),
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("uvicorn 未能在超时内启动")

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot

            # ---- 建队 + 一句话目标：自动闭环一轮跑完（至交付终态）----
            ev1: list = []
            r1 = await _rpc(ws, "t1", "chat.send", {
                "text": "目标：产出调研报告", "team": True,
                "director_mode": "ai", "director": {"provider": "fa", "model": "ma"},
                "members": [{"provider": "fm", "model": "mm", "name": "小研",
                             "persona": "研究员"}],
            }, events=ev1)
            assert r1.get("ok") is True, r1
            started = next(e for e in ev1 if e["event"] == "team_started")
            assert started["data"]["director_mode"] == "ai"
            assert started["data"]["director_provider"] == "fa"
            assert started["data"]["director_model"] == "ma"
            deltas = [e["data"] for e in ev1 if e["event"] == "team_message_delta"]
            assert any(d["member_index"] == -1 for d in deltas), "总管发言增量未到达"
            assert any(d["member_index"] == 0 for d in deltas), "队员发言增量未到达"
            finals = [e["data"] for e in ev1 if e["event"] == "team_message"]
            assert any(m["from_member"] == "director" and m["msg_kind"] == "ruling"
                       for m in finals)
            assert any(m["from_member"] == "小研" and m["msg_kind"] == "report"
                       for m in finals)
            assert not any(e["event"].startswith("permission_") for e in ev1)
            assert not any(e["event"].startswith("roundtable") for e in ev1)
            finished = next(e for e in ev1 if e["event"] == "team_finished")
            assert finished["data"]["status"] == "done"
            assert "《交付说明》" in finished["data"]["summary"]
            meta = r1["result"]["team"]["team"]
            assert meta["status"] == "done" and meta["rounds_used"] == 4
            assert meta["director"] == {
                "mode": "ai", "provider": "fa", "model": "ma",
                "input_tokens": 66, "output_tokens": 42, "stalled": [],
            }
            sid = r1["result"]["session_id"]

            # ---- 交付后恢复普通回合：消息不再进团队频道 ----
            ev2: list = []
            r2 = await _rpc(ws, "t2", "chat.send", {"text": "谢谢大家", "session_id": sid},
                            events=ev2)
            assert r2.get("ok") is True, r2
            assert r2["result"]["team"] is None
            assert not any(e["event"].startswith("team_") for e in ev2)
            assert "".join(e["data"].get("text", "") for e in ev2
                           if e["event"] == "text_delta") == "收队后的普通回答"
            g1 = await _rpc(ws, "g1", "team.get", {"session_id": sid})
            assert g1["result"]["active"] is False and g1["result"]["team"] is None
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

    # 落库：成员行与总管行分行入账，总管用量不挂任何队员名下（6 次调用 × 11/7）；
    # 末行是交付后普通回合的主模型（不属团队）
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute(
        "SELECT provider, model, in_tokens, out_tokens FROM usage_log"
    ).fetchall()
    assert rows == [("fm", "mm", 11, 7), ("fa", "ma", 66, 42), ("fake", "fake-1", 11, 7)]


async def test_real_socket_team_template_create_then_team_log_replay(home):
    """真实 socket 上跑通团队三期（模板建队 + 频道回放）的链路：
    存模板 → 列模板 → 用模板成员建队 → 收队 → team.log 全量回放 → 删模板。

    模板册 teams.json 与频道消息 team_messages 表都走 SKYSHEEP_HOME 隔离；
    team_id 经 meta 拿到、回放行 seq 升序且 session_id 戳建队会话——模板
    落盘、消息落库、归属校验任何一处接线错误都会在真实分帧上挂。"""
    import uvicorn

    (home / "home").mkdir(parents=True, exist_ok=True)
    pool = [
        FakeProvider([[TextBlock(text="收队后的普通回答")]]),  # 主模型（本用例不消耗）
        FakeProvider([[TextBlock(text="按模板完成调研")]]),  # 队员小研
    ]
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool else FakeProvider([]),
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("uvicorn 未能在超时内启动")

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot

            # ---- 存模板 → 列模板（全局册 teams.json）----
            s1 = await _rpc(ws, "s1", "team.template_save", {
                "name": "e2e调研小队",
                "members": [{"provider": "fa", "model": "ma", "name": "小研",
                             "persona": "研究员"}],
            })
            assert s1.get("ok") is True, s1
            assert s1["result"]["template"]["name"] == "e2e调研小队"
            l1 = await _rpc(ws, "l1", "team.template_list", {})
            assert [d["name"] for d in l1["result"]["templates"]] == ["e2e调研小队"]

            # ---- 从模板建队：模板成员照常拼进 chat.send ----
            ev1: list = []
            r1 = await _rpc(ws, "t1", "chat.send", {
                "text": "@小研 按模板开工", "team": True,
                "members": s1["result"]["template"]["members"],
            }, events=ev1)
            assert r1.get("ok") is True, r1
            started = next(e for e in ev1 if e["event"] == "team_started")
            assert [m["name"] for m in started["data"]["roster"]] == ["小研"]
            assert any(e["event"] == "team_message"
                       and e["data"]["from_member"] == "小研"
                       and "按模板完成调研" in e["data"]["text"] for e in ev1)
            sid = r1["result"]["session_id"]
            team_id = r1["result"]["team"]["team"]["team_id"]
            assert len(team_id) == 32

            # ---- 收队 → team.log 全量回放（收队后仍凭落库行归属放行）----
            s2 = await _rpc(ws, "s2", "team.stop",
                            {"session_id": sid, "reason": "收"})
            assert s2.get("ok") is True, s2
            g1 = await _rpc(ws, "g1", "team.log", {"team_id": team_id})
            assert g1.get("ok") is True, g1
            assert g1["result"]["team_id"] == team_id
            assert g1["result"]["session_id"] == sid
            rows = g1["result"]["messages"]
            assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
            assert rows[0]["from_member"] == "user" and rows[0]["msg_kind"] == "ruling"
            assert any(r["from_member"] == "小研" and r["msg_kind"] == "report"
                       for r in rows)
            assert all(r["session_id"] == sid for r in rows)

            # ---- 删模板收尾 ----
            d1 = await _rpc(ws, "d1", "team.template_remove", {"name": "e2e调研小队"})
            assert d1.get("ok") is True and d1["result"] == {"name": "e2e调研小队"}
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

    # 落盘对账：teams.json 删空；team_messages 行带建队会话 id（text 全文落库）
    teams = json.loads((home / "home" / "teams.json").read_text(encoding="utf-8"))
    assert teams == {"templates": []}
    db = sqlite3.connect(home / "home" / "skysheep.db")
    try:
        db_rows = db.execute(
            "SELECT session_id, from_member, msg_kind, text FROM team_messages"
            " WHERE team_id = ? ORDER BY seq", (team_id,),
        ).fetchall()
    finally:
        db.close()
    assert db_rows[0] == (sid, "user", "ruling", "@小研 按模板开工")
    assert any(r[1] == "小研" and r[2] == "report" for r in db_rows)
