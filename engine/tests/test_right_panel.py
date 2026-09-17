"""右侧标签页面板：终端（term.run/term.stop）、辅助对话（chat.aux）、
审查（checkpoint.diff）、以及界面偏好的标签持久化（right_tabs 等）。"""

from __future__ import annotations

import sys

from test_server import make_client, recv_until

from skysheep.messages import TextBlock, ToolUseBlock


def test_terminal_run_streams_and_done(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "term.run", "params": {"command": "echo sky_sheep_ok"}})
        events = []
        frame = recv_until(ws, "t1", events)
        assert frame["ok"], frame
        chunks = "".join(
            e["data"]["text"] for e in events if e["event"] == "terminal_chunk"
        )
        assert "sky_sheep_ok" in chunks
        done = [e for e in events if e["event"] == "terminal_done"]
        assert done and done[0]["data"]["code"] == 0
        # 跑完之后应回到空闲，可以再跑下一条
        ws.send_json({"id": "t2", "method": "term.run", "params": {"command": "echo again"}})
        frame2 = recv_until(ws, "t2")
        assert frame2["ok"]


def test_terminal_busy_rejected_then_stop(home):
    long_cmd = "ping -n 30 127.0.0.1" if sys.platform == "win32" else "sleep 30"
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "term.run", "params": {"command": long_cmd}})
        # 运行中第二条直接被拒
        ws.send_json({"id": "t2", "method": "term.run", "params": {"command": "echo x"}})
        busy = recv_until(ws, "t2")
        assert not busy["ok"] and "已有命令在运行" in busy["error"]
        # 停止 → 第一条立刻收尾且标记 stopped
        ws.send_json({"id": "s1", "method": "term.stop"})
        stop = recv_until(ws, "s1")
        assert stop["ok"] and stop["result"]["stopped"] is True
        frame = recv_until(ws, "t1")
        assert frame["ok"] and frame["result"]["stopped"] is True
        # 进程树已被杀，可以再次运行
        ws.send_json({"id": "t3", "method": "term.run", "params": {"command": "echo after_stop"}})
        events = []
        frame3 = recv_until(ws, "t3", events)
        assert frame3["ok"]
        assert "after_stop" in "".join(
            e["data"]["text"] for e in events if e["event"] == "terminal_chunk"
        )


def test_terminal_empty_command_rejected(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "term.run", "params": {"command": "   "}})
        frame = recv_until(ws, "t1")
        assert not frame["ok"]


def test_chat_aux_streams_and_keeps_history(home):
    from skysheep.messages import Message  # noqa: F401
    from skysheep.models.fake import FakeProvider

    prov = FakeProvider([[TextBlock(text="辅助回答")], [TextBlock(text="第二次回答")]])
    with make_client(home, [], provider=prov) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "chat.aux", "params": {"text": "侧栏问题"}})
        events = []
        frame = recv_until(ws, "a1", events)
        assert frame["ok"] and frame["result"]["text"] == "辅助回答"
        deltas = "".join(e["data"]["text"] for e in events if e["event"] == "aux_delta")
        assert "辅助回答" in deltas

        ws.send_json({"id": "a2", "method": "chat.aux", "params": {"text": "再问一句"}})
        frame = recv_until(ws, "a2")
        assert frame["ok"] and frame["result"]["text"] == "第二次回答"

        # 第二次调用应带上第一轮问答 + system 开头（辅助对话有独立记忆）
        second_call = prov.calls[1]
        assert second_call[0].role == "system"
        roles = [m.role for m in second_call]
        assert roles == ["system", "user", "assistant", "user"]

        ws.send_json({"id": "c1", "method": "aux.clear"})
        frame = recv_until(ws, "c1")
        assert frame["ok"] and frame["result"]["cleared"] is True
        # 清空后再问：只带 system + 本条消息
        ws.send_json({"id": "a3", "method": "chat.aux", "params": {"text": "新话题"}})
        recv_until(ws, "a3")
        third_call = prov.calls[2]
        assert [m.role for m in third_call] == ["system", "user"]


def test_chat_aux_requires_provider(home):
    # 不注入 provider 工厂 → provider 为 None → 给出可读错误
    from fastapi.testclient import TestClient

    from skysheep.server import create_app

    app = create_app(working_dir=home / "proj")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "chat.aux", "params": {"text": "hi"}})
        frame = recv_until(ws, "a1")
        assert not frame["ok"] and "模型服务未配置" in frame["error"]


def test_checkpoint_diff_shows_changes(home):
    """两轮写同一文件：第一轮新建（created），第二轮编辑（modified，含 -/+ 行）。"""
    script = [
        [ToolUseBlock(id="t1", name="write_file", input={"path": "rev.txt", "content": "line1\nline2\n"})],
        [TextBlock(text="第一轮写好了")],
        [ToolUseBlock(
            id="t2", name="edit_file",
            input={"path": "rev.txt", "old_string": "line2", "new_string": "line2 改过"},
        )],
        [TextBlock(text="第二轮改好了")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "第一轮：新建文件"}})
        ws.send_json({"id": "c2", "method": "chat.send", "params": {"text": "第二轮：再编辑一次"}})
        # 两轮排队执行，逐帧读：回应权限请求，直到两轮都完成
        done_ids = set()
        while done_ids != {"c1", "c2"}:
            fr = ws.receive_json()
            if "event" in fr:
                if fr["event"] == "permission_request":
                    ws.send_json({
                        "id": "p" + fr["data"]["request_id"],
                        "method": "permission.respond",
                        "params": {"request_id": fr["data"]["request_id"], "decision": "allow_once"},
                    })
                continue
            if fr.get("id") in ("c1", "c2"):
                assert fr["ok"], fr
                done_ids.add(fr["id"])
        # 两轮各自落了检查点；用 checkpoint.list 取到本轮会话的全部检查点
        ws.send_json({"id": "l1", "method": "checkpoint.list"})
        listing = recv_until(ws, "l1")["result"]["checkpoints"]
        assert len(listing) == 2, f"应有两条检查点，实际 {listing}"
        newest = listing[-1]["id"]  # FIFO 追加，最后一条是第二轮
        ws.send_json({"id": "d1", "method": "checkpoint.diff", "params": {"id": newest}})
        diff = recv_until(ws, "d1")["result"]
        f = diff["files"][0]
        assert f["path"].endswith("rev.txt")
        assert f["status"] == "modified"
        assert "-line2" in f["diff"] and "+line2 改过" in f["diff"]
        # 第一条（新建）检查点是 created
        ws.send_json({"id": "d2", "method": "checkpoint.diff", "params": {"id": listing[0]["id"]}})
        first = recv_until(ws, "d2")["result"]["files"][0]
        assert first["status"] == "created" and "+line1" in first["diff"]
        # 不存在的检查点给出可读错误
        ws.send_json({"id": "d3", "method": "checkpoint.diff", "params": {"id": "cp999"}})
        bad = recv_until(ws, "d3")
        assert not bad["ok"] and "不存在或已过期" in bad["error"]


def test_ui_prefs_right_panel_keys(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({
            "id": "u1",
            "method": "ui.save",
            "params": {"prefs": {
                "right_w": 380,
                "right_tabs": ["terminal", "aux"],
                "right_active": "terminal",
            }},
        })
        frame = recv_until(ws, "u1")
        assert frame["ok"]
        assert frame["result"]["prefs"] == {
            "right_w": 380,
            "right_tabs": ["terminal", "aux"],
            "right_active": "terminal",
        }
        # 非法标签 id 被过滤、去重；active 非法被清掉；right_w 越界收敛
        ws.send_json({
            "id": "u2",
            "method": "ui.save",
            "params": {"prefs": {
                "right_w": 9999,
                "right_tabs": ["evil", "aux", "aux"],
                "right_active": "evil",
            }},
        })
        frame = recv_until(ws, "u2")
        assert frame["result"]["prefs"] == {"right_w": 720, "right_tabs": ["aux"]}
        # null 删除
        ws.send_json({
            "id": "u3",
            "method": "ui.save",
            "params": {"prefs": {"right_tabs": None, "right_active": None, "right_w": None}},
        })
        frame = recv_until(ws, "u3")
        assert frame["result"]["prefs"] == {}
        # 面板收起标志（0/1）
        ws.send_json({"id": "u4", "method": "ui.save", "params": {"prefs": {"right_collapsed": 1}}})
        frame = recv_until(ws, "u4")
        assert frame["result"]["prefs"] == {"right_collapsed": 1}
        ws.send_json({"id": "u5", "method": "ui.save", "params": {"prefs": {"right_collapsed": 0}}})
        frame = recv_until(ws, "u5")
        assert frame["result"]["prefs"] == {"right_collapsed": 0}
        # 「项目记忆」也是合法标签：可持久化，切会话/重启后能恢复
        ws.send_json({
            "id": "u6",
            "method": "ui.save",
            "params": {"prefs": {"right_tabs": ["memory", "cron"], "right_active": "memory"}},
        })
        frame = recv_until(ws, "u6")
        # right_collapsed 沿用上一步的值，这里只断言本步写入的三项
        prefs = frame["result"]["prefs"]
        assert prefs["right_tabs"] == ["memory", "cron"]
        assert prefs["right_active"] == "memory"
        # 读回验证落盘：重启后也能恢复
        ws.send_json({"id": "u-read", "method": "ui.get"})
        stored = recv_until(ws, "u-read")["result"]["prefs"]
        assert stored["right_tabs"] == ["memory", "cron"]
        assert stored["right_active"] == "memory"
