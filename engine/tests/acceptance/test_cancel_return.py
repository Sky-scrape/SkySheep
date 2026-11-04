"""G 组 · 取消 / 返回 / 重试验收。

用户点「停止」、拒掉确认卡、断线重连、编排叫停、失败重发——这些路径最容易
出现「UI 停了但后台还在跑」「后台停了但界面卡住」。每条都断言终态与可继续性。
"""

from __future__ import annotations

import pytest
from _helpers import SlowProvider, make_client, rpc, wait_until

from skysheep.messages import TextBlock, ToolUseBlock

pytestmark = pytest.mark.acceptance

SLOW_TEXT = "正在逐个字符地整理这份很长的报告，请稍候。" * 12


def test_g1_stop_midstream_keeps_partial_and_session_stays_usable(home):
    """G1 流式中途停止：保留已输出文本、终态 cancelled、还能继续发下一条。"""
    scripts = [
        SlowProvider(SLOW_TEXT, chunk=4, delay_s=0.01),
        [[TextBlock(text="普通回答")]],  # 第二个会话用正常速度，验证停止后不假死
    ]
    with make_client(home, scripts) as client, \
            client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new")["result"]["id"]
        ws.send_json({"id": "c1", "method": "chat.send",
                      "params": {"text": "写一份长报告", "session_id": sid}})
        evs: list = []
        stop_sent = False
        stop_resp = None
        final = None
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                evs.append(frame)
                if not stop_sent and frame["event"] == "text_delta":
                    ws.send_json({"id": "s1", "method": "stop"})
                    stop_sent = True
                continue
            if frame.get("id") == "s1":
                stop_resp = frame
                continue
            if frame.get("id") == "c1":
                final = frame
                break

        assert stop_sent, "应该能收到流式增量后再停止"
        assert stop_resp and stop_resp["result"]["cancelled"] is True
        assert final["ok"] and final["result"]["stopped"] is True
        deltas = "".join(
            e["data"].get("text", "") for e in evs if e["event"] == "text_delta"
        )
        assert deltas, "停止前已输出的内容不能凭空消失"
        assert "turn_finished" not in [
            e["event"] for e in evs
        ], "被取消的轮以 stopped 收尾，不伪造 turn_finished"

        # 停止后仍能开新会话正常跑完（不是假死）
        sid2 = rpc(ws, "n2", "session.new")["result"]["id"]
        again = rpc(ws, "c2", "chat.send", {"text": "继续", "session_id": sid2})
        assert again["ok"] and again["result"]["stopped"] is False
        assert again["result"]["done"] is True
        msgs = rpc(ws, "p1", "session.peek_messages", {"id": sid2})["result"]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"], "新一轮的消息要正常落库"


def test_g2_stop_while_permission_pending_clears_card(home):
    """G2 确认卡等待中停止：请求作废、不落盘、收尾补发 cancelled 决策。"""
    with make_client(home, [[
        [ToolUseBlock(id="w", name="write_file", input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="不写了")],
    ]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        evs: list = []
        stop_resp = None
        final = None
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                evs.append(frame)
                if frame["event"] == "permission_request":
                    ws.send_json({"id": "s1", "method": "stop"})
                continue
            if frame.get("id") == "s1":
                stop_resp = frame
                continue
            if frame.get("id") == "c1":
                final = frame
                break

        assert stop_resp and stop_resp["result"]["cancelled"] is True
        assert final["result"]["stopped"] is True
        assert not (home / "proj" / "a.txt").exists(), "取消后绝不能落盘"
        resolved = [e["data"] for e in evs if e["event"] == "permission_resolved"]
        assert resolved and resolved[-1]["decision"] == "cancelled", \
            "残留在屏上的确认卡必须被明确作废"


def test_g3_duplicate_permission_response_does_not_flip_decision(home):
    """G3 重复回答同一张确认卡：第一次决策生效，后续投递不能翻转结果。

    接口层对「已投递但尚未收尾」的请求仍可能回 delivered=true（是否真生效由
    第一次决策决定）——所以这里断言的是效果幂等：先拒绝后再点允许，文件仍不落盘。
    """
    with make_client(home, [[
        [ToolUseBlock(id="w", name="write_file", input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="写完了")],
    ]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        first = second = None
        final = None
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                if frame["event"] == "permission_request" and first is None:
                    rid = frame["data"]["request_id"]
                    ws.send_json({"id": "p1", "method": "permission.respond",
                                  "params": {"request_id": rid, "decision": "deny"}})
                    ws.send_json({"id": "p2", "method": "permission.respond",
                                  "params": {"request_id": rid, "decision": "allow_once"}})
                continue
            if frame.get("id") == "p1":
                first = frame
                continue
            if frame.get("id") == "p2":
                second = frame
                continue
            if frame.get("id") == "c1":
                final = frame
                break
        assert first["result"]["delivered"] is True
        assert second["ok"] is True, "重复投递不应把连接打成错误"
        assert not (home / "proj" / "a.txt").exists(), \
            "第一次是拒绝，后续「允许」不能翻转已生效的决策"
        assert final["ok"] and final["result"]["done"]


async def test_g4_client_gone_queued_permission_still_answered(home):
    """G4 前端掉线：未决请求仍在队列里可重建，回答后原轮继续跑完并落盘。

    用引擎入口直接验证（不经 WS 发送层）：客户端消失不能把这一轮变成假死。
    """
    import asyncio

    from skysheep.models.fake import FakeProvider
    from skysheep.server._dispatch_chat import _h_permission_queue
    from skysheep.server.backend import ServerBackend

    provider = FakeProvider([
        [ToolUseBlock(id="w", name="write_file", input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="写完了")],
    ])
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: provider)
    await be.setup()
    evs: list = []

    async def emit(ev):
        evs.append(ev)

    task = asyncio.create_task(be.send("写文件", emit))
    try:
        for _ in range(300):
            reqs = [e for e in evs if e.get("kind") == "permission_request"]
            if reqs:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("没有等到权限请求")

        rid = reqs[0]["request_id"]
        queue = await _h_permission_queue(be, {}, emit, True)
        assert any(p["request_id"] == rid for p in queue["pending"]), \
            "掉线后重建的确认卡必须还找得到这条未决请求"

        assert be.respond_permission(rid, "allow_once") is True
        result = await asyncio.wait_for(task, timeout=10)
        assert result["done"] is True
        assert (home / "proj" / "a.txt").read_text(encoding="utf-8") == "x"
    finally:
        await be.shutdown()


def test_g5_pipeline_cancel_stops_running_node(home):
    """G5 编排叫停：运行中的节点被取消，流水线进 cancelled，不再继续派下游。"""
    from fastapi.testclient import TestClient

    from skysheep.server import create_app

    slow = SlowProvider("长任务输出" * 200, chunk=4, delay_s=0.01)
    app = create_app(working_dir=home / "proj", provider_name="fake",
                     provider_factory=lambda: slow)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        pipe = rpc(ws, "c1", "pipeline.create", {
            "name": "会被叫停", "nodes": [
                {"title": "长节点", "prompt": "慢慢写"},
                {"title": "下游", "prompt": "接着做", "after": [0]},
            ],
        })["result"]["pipeline"]
        assert rpc(ws, "s1", "pipeline.start", {"id": pipe["id"]})["ok"]
        wait_until(
            lambda: rpc(ws, "g1", "pipeline.get", {"id": pipe["id"]})
            ["result"]["pipeline"]["status"] == "running",
            timeout=10,
        )
        out = rpc(ws, "x1", "pipeline.cancel", {"id": pipe["id"]})["result"]
        assert out["cancelled"] is True
        final = wait_until(
            lambda: (lambda p: p if p["status"] == "cancelled"
                     and all(n["status"] != "running" for n in p["nodes"]) else None)(
                rpc(ws, "g2", "pipeline.get", {"id": pipe["id"]})["result"]["pipeline"]
            ),
            timeout=10,
        )
        by_title = {n["title"]: n for n in final["nodes"]}
        assert by_title["长节点"]["status"] == "cancelled"
        assert by_title["下游"]["status"] == "cancelled", "下游不应再被派发"


def test_g6_retry_only_for_failed_or_interrupted_runs(home):
    """G6 重发：中断的主轮拿到原文可重发；已完成/不存在的运行明确拒绝。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        be = client.app.state.backend
        sid = rpc(ws, "n1", "session.new")["result"]["id"]

        be.runs.register("run-int", "turn", sid, retry_text="重新跑这一句")
        be.runs.update("run-int", state="interrupted")
        out = rpc(ws, "r1", "runs.retry", {"run_id": "run-int"})["result"]
        assert out["kind"] == "turn" and out["text"] == "重新跑这一句"
        assert out["session_id"] == sid

        # 用原文重发，得到正常回答（前端「重发」按钮的等价动作）
        assert rpc(ws, "c1", "chat.send",
                   {"text": out["text"], "session_id": sid})["ok"]

        be.runs.register("run-done", "turn", sid, retry_text="已经跑完")
        be.runs.update("run-done", state="completed")
        denied = rpc(ws, "r2", "runs.retry", {"run_id": "run-done"})
        assert denied["ok"] is False and "不可重发" in denied["error"]
        missing = rpc(ws, "r3", "runs.retry", {"run_id": "nope"})
        assert missing["ok"] is False and "不可重发" in missing["error"]
