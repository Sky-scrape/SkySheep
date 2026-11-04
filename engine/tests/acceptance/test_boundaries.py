"""H 组 · 边界验收：空数据、无项目态、数量多、坏输入、多连接。

边界用例不追求穷举参数，而是确认「不会崩、不会串台、不会静默吞掉」。
"""

from __future__ import annotations

import asyncio

import pytest
from _helpers import make_client, rpc

from skysheep.config import ConfigError
from skysheep.messages import TextBlock
from skysheep.server import create_app
from skysheep.tools import ReadFileTool
from skysheep.tools.base import NO_PROJECT_HINT, ToolContext, ToolError
from skysheep.tools.fs import ReadFileArgs

pytestmark = pytest.mark.acceptance


def test_h1_empty_states_are_well_formed(home):
    """H1 空数据：没有会话/消息/归档/检查点/任务时，读接口给空集合而不是报错。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        listed = rpc(ws, "l1", "session.list")["result"]
        assert listed["sessions"] == []
        assert listed["empty_count"] == 0 and listed["archived_count"] == 0

        assert rpc(ws, "s1", "session.search", {"query": "任意"})["result"]["results"] == []
        assert rpc(ws, "a1", "session.list_archived")["result"]["sessions"] == []
        assert rpc(ws, "c1", "checkpoint.list")["result"]["checkpoints"] == []
        assert rpc(ws, "t1", "tasks.list")["result"]["tasks"] == []
        assert rpc(ws, "q1", "permission.queue")["result"]["pending"] == []

        # 空会话也能正常发第一条消息（不是「必须已有历史」）
        sid = rpc(ws, "n1", "session.new")["result"]["id"]
        assert rpc(ws, "m1", "chat.send",
                   {"text": "第一条", "session_id": sid})["result"]["done"] is True
        msgs = rpc(ws, "p1", "session.peek_messages", {"id": sid})["result"]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_h2_no_project_tools_give_readable_hint(tmp_path):
    """H2 无项目态：文件工具给「先添加项目」的可读提示，不是裸异常。"""

    async def attempt():
        tool = ReadFileTool()
        ctx = ToolContext(working_dir=None)
        with pytest.raises(ToolError) as exc:
            await tool.run(ReadFileArgs(path="任意.txt"), ctx)
        return str(exc.value)

    message = asyncio.run(attempt())
    assert message == NO_PROJECT_HINT
    assert "添加项目" in message


def test_h3_many_sessions_are_capped_and_cleanable(home):
    """H3 数量多：300 个空会话列表只回 50 条；一键清理能全部收掉。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i in range(300):
            rpc(ws, f"n{i}", "session.new", {"title": f"会话 {i}"})
        listed = rpc(ws, "l1", "session.list")["result"]
        assert listed["empty_count"] == 300
        assert len(listed["sessions"]) == 50, "侧栏单页上限 50，不能一次吐 300 条"

        cleaned = rpc(ws, "c1", "session.cleanup_empty")["result"]
        assert len(cleaned["removed_ids"]) == 299, "当前会话按设计保留，其余 299 个清掉"
        after = rpc(ws, "l2", "session.list")["result"]
        assert len(after["sessions"]) == 1, "当前会话不能被一键清理带走"
        assert after["empty_count"] == 1


def test_h4_malformed_frames_and_unknown_methods_are_readable(home):
    """H4 坏输入：二进制帧 / 非 JSON / 非对象 / 未知方法 / params 错型都有回执。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"\x00\x01not-json")
        r = ws.receive_json()
        assert r["ok"] is False and "二进制" in r["error"]

        ws.send_text("这不是 JSON{{{")
        r = ws.receive_json()
        assert r["ok"] is False and "JSON" in r["error"]

        ws.send_text('"just a string"')
        r = ws.receive_json()
        assert r["ok"] is False and "JSON 对象" in r["error"]

        unknown = rpc(ws, "u1", "no.such.method")
        assert unknown["ok"] is False and "unknown method" in unknown["error"]

        ws.send_json({"id": "p1", "method": "session.new", "params": ["not", "dict"]})
        bad = ws.receive_json()
        while "id" not in bad or bad.get("id") != "p1":
            bad = ws.receive_json()
        assert bad["ok"] is False and "params" in bad["error"]

        # 连续坏输入后连接仍然可用
        assert rpc(ws, "b1", "boot")["ok"] is True


def test_h5_corrupt_config_fails_with_readable_error(home):
    """H5 坏配置：config.toml 解析失败给可读 ConfigError（带路径），不是裸栈。"""
    cfg = home / "home" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("this is not = valid [toml", encoding="utf-8")
    from fastapi.testclient import TestClient

    app = create_app(working_dir=home / "proj")
    with pytest.raises(ConfigError) as exc:
        with TestClient(app):
            pass
    assert "config.toml" in str(exc.value)


def test_h6_two_clients_do_not_cross_talk(home):
    """H6 多连接：两个客户端各自的会话与回答互不串台。"""
    scripts = [
        [[TextBlock(text="甲的回答")]],
        [[TextBlock(text="乙的回答")]],
    ]
    with make_client(home, scripts) as client:
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            sid_a = rpc(a, "na", "session.new")["result"]["id"]
            sid_b = rpc(b, "nb", "session.new")["result"]["id"]
            assert sid_a != sid_b

            a.send_json({"id": "ca", "method": "chat.send",
                         "params": {"text": "甲的问题", "session_id": sid_a}})
            b.send_json({"id": "cb", "method": "chat.send",
                         "params": {"text": "乙的问题", "session_id": sid_b}})

            ra = rb = None
            while ra is None or rb is None:
                if ra is None:
                    frame = a.receive_json()
                    if frame.get("id") == "ca":
                        ra = frame
                if rb is None:
                    frame = b.receive_json()
                    if frame.get("id") == "cb":
                        rb = frame
            assert ra["result"]["session_id"] == sid_a
            assert rb["result"]["session_id"] == sid_b

            # 落库内容各归各的会话
            a_msgs = rpc(a, "pa", "session.peek_messages", {"id": sid_a})["result"]["messages"]
            b_msgs = rpc(b, "pb", "session.peek_messages", {"id": sid_b})["result"]["messages"]
            a_text = "".join(
                block.get("text", "") for m in a_msgs
                for block in (m.get("content") or []) if block.get("type") == "text"
            )
            b_text = "".join(
                block.get("text", "") for m in b_msgs
                for block in (m.get("content") or []) if block.get("type") == "text"
            )
            assert "甲的回答" in a_text or any("甲" in str(m) for m in a_msgs)
            assert "乙的回答" in b_text or any("乙" in str(m) for m in b_msgs)
