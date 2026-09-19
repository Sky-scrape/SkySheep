"""底部终端面板（PTY 真终端：term.spawn/input/resize/close）、辅助对话（chat.aux）、
审查（checkpoint.diff）、以及界面偏好的标签持久化（right_tabs 等）。"""

from __future__ import annotations

import sys
import time

import pytest
from test_server import make_client, recv_until

from skysheep.messages import TextBlock, ToolUseBlock

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="ConPTY 终端仅 Windows")


@WIN_ONLY
def test_terminal_spawn_input_output(home):
    """PTY 终端：spawn 后敲命令，输出经 term_data 事件流回传。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "term.spawn",
                      "params": {"term_id": "tab-a", "rows": 24, "cols": 100}})
        assert recv_until(ws, "s1")["ok"]
        ws.send_json({"id": "i1", "method": "term.input",
                      "params": {"term_id": "tab-a", "data": "echo sky_pty_a\r"}})
        assert recv_until(ws, "i1")["ok"]
        ws.send_json({"id": "i2", "method": "term.input",
                      "params": {"term_id": "tab-a", "data": "echo sky_pty_b\r"}})
        assert recv_until(ws, "i2")["ok"]
        # PTY 输出是单条有序流；PowerShell 冷启动在本机（Defender 实时扫描）可到
        # 4~5 秒，轮询窗口放宽到 15 秒避免环境性误报
        events = []
        found = False
        for i in range(150):
            chunks = "".join(
                e["data"]["text"] for e in events if e["event"] == "term_data"
            )
            if "sky_pty_a" in chunks and "sky_pty_b" in chunks:
                found = True
                break
            time.sleep(0.1)
            ws.send_json({"id": f"w{i}", "method": "term.resize",
                          "params": {"term_id": "tab-a", "rows": 24, "cols": 100}})
            recv_until(ws, f"w{i}", events)
        assert found, "echo 输出应经 term_data 事件回传"


@WIN_ONLY
def test_terminal_tabs_are_independent(home):
    """两个标签各自一条 PTY 流（term_id 路由互不串线）；关标签广播 term_exit。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for tid in ("tab-a", "tab-b"):
            ws.send_json({"id": "s-" + tid, "method": "term.spawn",
                          "params": {"term_id": tid, "rows": 24, "cols": 100}})
            assert recv_until(ws, "s-" + tid)["ok"]
        ws.send_json({"id": "a1", "method": "term.input",
                      "params": {"term_id": "tab-a", "data": "echo mark_from_a\r"}})
        assert recv_until(ws, "a1")["ok"]
        ws.send_json({"id": "b1", "method": "term.input",
                      "params": {"term_id": "tab-b", "data": "echo mark_from_b\r"}})
        assert recv_until(ws, "b1")["ok"]

        events = []
        a_ok = b_ok = False
        # 两个 PowerShell 冷启动叠加可到 6 秒以上，窗口放宽到 15 秒避免环境性误报
        for i in range(150):
            ws.send_json({"id": f"w{i}", "method": "term.resize",
                          "params": {"term_id": "tab-b", "rows": 24, "cols": 100}})
            recv_until(ws, f"w{i}", events)
            chunks = "".join(
                e["data"]["text"] for e in events if e["event"] == "term_data"
            )
            a_ok = "mark_from_a" in chunks
            b_ok = "mark_from_b" in chunks
            if a_ok and b_ok:
                break
            time.sleep(0.1)
        assert a_ok and b_ok
        # 每个标签的流里只有自己的标记（term_id 路由正确、互不串线）
        a_only = "".join(
            e["data"]["text"] for e in events
            if e["event"] == "term_data" and e["data"].get("term_id") == "tab-a"
        )
        b_only = "".join(
            e["data"]["text"] for e in events
            if e["event"] == "term_data" and e["data"].get("term_id") == "tab-b"
        )
        assert "mark_from_a" in a_only and "mark_from_b" not in a_only
        assert "mark_from_b" in b_only and "mark_from_a" not in b_only

        # 关闭 tab-a：term_exit 广播；tab-b 照常可用
        ws.send_json({"id": "c1", "method": "term.close", "params": {"term_id": "tab-a"}})
        assert recv_until(ws, "c1")["ok"]
        exited = False
        for i in range(15, 165):
            ws.send_json({"id": f"w{i}", "method": "term.resize",
                          "params": {"term_id": "tab-b", "rows": 24, "cols": 100}})
            recv_until(ws, f"w{i}", events)
            if any(
                e["event"] == "term_exit" and e["data"].get("term_id") == "tab-a"
                for e in events
            ):
                exited = True
                break
            time.sleep(0.1)
        assert exited, "关闭标签后应广播 term_exit"
        ws.send_json({"id": "b2", "method": "term.input",
                      "params": {"term_id": "tab-b", "data": "echo still_ok\r"}})
        assert recv_until(ws, "b2")["ok"]


@WIN_ONLY
def test_terminal_tab_cap(home):
    """标签数有上限：开满 MAX_TERMINALS 后再开新的要被拒。"""
    from skysheep.server.backend import TerminalManager

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i in range(TerminalManager.MAX_TERMINALS):
            ws.send_json({"id": f"r{i}", "method": "term.spawn",
                          "params": {"term_id": f"cap-{i}", "rows": 24, "cols": 100}})
            assert recv_until(ws, f"r{i}")["ok"]
        ws.send_json({"id": "over", "method": "term.spawn",
                      "params": {"term_id": "cap-over", "rows": 24, "cols": 100}})
        frame = recv_until(ws, "over")
        assert not frame["ok"] and "最多" in frame["error"]


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
