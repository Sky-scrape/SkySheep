"""底部终端面板（PTY 真终端：term.spawn/input/resize/close）、辅助对话（chat.aux）、
审查（checkpoint.diff）、项目记忆读写（编码安全 / 截断明示 / 切项目重置 aux 历史）、
以及界面偏好的标签持久化（right_tabs 等）。"""

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


def test_aux_history_resets_on_project_switch(home):
    """切项目必须重置辅助对话历史：system 消息里的 cwd 只在历史为空时注入，
    不清掉的话切项目后模型仍以为在上一个目录里。"""
    from skysheep.models.fake import FakeProvider

    prov = FakeProvider([[TextBlock(text="答一")], [TextBlock(text="答二")]])
    other = home / "other"
    other.mkdir()
    with make_client(home, [], provider=prov) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "chat.aux", "params": {"text": "问一"}})
        assert recv_until(ws, "a1")["ok"]
        ws.send_json({"id": "s1", "method": "project.switch",
                      "params": {"path": str(other)}})
        frame = recv_until(ws, "s1")
        assert frame["ok"] and frame["result"]["switched"]
        ws.send_json({"id": "a2", "method": "chat.aux", "params": {"text": "问二"}})
        assert recv_until(ws, "a2")["ok"]
        # 切项目后的第一问：历史被重置 → 只带新的 system（含新目录）+ 本条消息
        second = prov.calls[1]
        assert [m.role for m in second] == ["system", "user"]
        assert str(other) in second[0].text
        assert str(home / "proj") not in second[0].text


def test_project_instructions_encoding_and_truncation(home):
    """项目记忆读写走 textio：GBK 的 AGENTS.md 不再被读成替换字符、保存保编码；
    超上限截断随结果明示（truncated / original_chars / limit），不再静默。"""
    from skysheep.core.prompt import MAX_INSTRUCTIONS_CHARS

    proj = home / "proj"
    agents = proj / "AGENTS.md"
    agents.write_bytes("提交信息用中文\n改完必须跑测试\n".encode("gb18030"))

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g1", "method": "project.instructions"})
        got = recv_until(ws, "g1")["result"]
        assert "提交信息用中文" in got["text"], "GBK 内容应正确解码"
        assert "\ufffd" not in got["text"], "不得出现替换字符"
        assert got["encoding_text"] == "GB18030"
        assert got["editable"] is True

        ws.send_json({"id": "s1", "method": "project.save_instructions",
                      "params": {"text": "提交信息用中文\n新增一条约定\n",
                                 "base_mtime": got["mtime"]}})
        saved = recv_until(ws, "s1")["result"]
        assert saved["saved"] and not saved["truncated"]
        # 写回保留 GB18030：磁盘字节仍按 GBK 解得回（没有被悄悄换成 UTF-8）
        assert "新增一条约定" in agents.read_bytes().decode("gb18030")
        assert agents.read_bytes() != "提交信息用中文\n新增一条约定\n".encode()

        # 超上限：截断落盘，且 truncated / original_chars / limit 如实带回
        over = 123
        big = "字" * (MAX_INSTRUCTIONS_CHARS + over)
        ws.send_json({"id": "s2", "method": "project.save_instructions",
                      "params": {"text": big, "base_mtime": saved["mtime"]}})
        saved2 = recv_until(ws, "s2")["result"]
        assert saved2["truncated"] is True
        assert saved2["chars"] == MAX_INSTRUCTIONS_CHARS
        assert saved2["original_chars"] == MAX_INSTRUCTIONS_CHARS + over
        assert saved2["limit"] == MAX_INSTRUCTIONS_CHARS
        # 落盘的仍是 GB18030（沿用原编码）：按原编码解出 8000 字
        assert len(agents.read_bytes().decode("gb18030")) == MAX_INSTRUCTIONS_CHARS


def test_load_project_instructions_gbk(tmp_path):
    """系统提示词注入侧同样走 textio：GBK 的 AGENTS.md 不再把替换字符带进每轮提示词。"""
    from skysheep.core.prompt import load_project_instructions

    p = tmp_path / "AGENTS.md"
    p.write_bytes("约定内容".encode("gb18030"))
    path, text = load_project_instructions(tmp_path)
    assert path == str(p)
    assert "约定内容" in text and "\ufffd" not in text


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


def test_rp_tabs_compress_no_scroll(home):
    """标签条压缩呈现：不横向滚动，标签均分宽度、逐级降级（藏图标/藏非激活关闭钮）。

    标签一多以前靠 overflow-x: auto 出滚动条——滚动条占面板高度且难拖；
    改为均分 + 省略号 + title 提示，降级档位由 JS 渲染后按均分宽度判定。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    # CSS：容器不再横向滚动；标签可压缩、文字截断；两档降级类存在
    tabs_block = css.split("#rp-tabs {")[1].split("}")[0]
    assert "overflow-x: auto" not in tabs_block, "标签条不得再横向滚动"
    tab_block = css.split(".rp-tab {")[1].split("}")[0]
    assert "flex: 1 1 0" in tab_block and "min-width: 0" in tab_block
    span_rule = css.split(".rp-tab > span:not(.rp-x) {")[1].split("}")[0]
    assert "text-overflow: ellipsis" in span_rule
    assert "#rp-tabs.rp-tight" in css and "#rp-tabs.rp-cramped" in css
    assert "#rp-tabs.rp-icon" in css, "更挤时有只显图标的档位"
    # JS：档位判定抽成 applyRpTabDensity，渲染与容器宽度变化（ResizeObserver）共用；
    # 图标档与其它两档互斥（整格只剩图标时 tight/cramped 规则无意义）
    assert "function applyRpTabDensity(" in js
    assert 'classList.toggle("rp-icon", iconOnly)' in js
    assert 'classList.toggle("rp-tight", !iconOnly && per < 92)' in js
    assert 'classList.toggle("rp-cramped", !iconOnly && per < 64)' in js
    assert "ResizeObserver" in js, "拖面板宽度后档位要即时跟上"
    # RO 回调必须 rAF 推迟 + 档位无变化不写 DOM：回调内同步改布局会刷
    # 「ResizeObserver loop completed」告警，被全局错误横幅接住吓到用户
    # （与行卡 RO 同一套做法，见 mountPipelineGraph 注释）
    assert "requestAnimationFrame(() => applyRpTabDensity())" in js
    assert "if (key === _rpDensityKey) return" in js
    # 错误陷阱忽略该浏览器良性告警（真循环的根因在各自 RO 回调里修）
    assert 'indexOf("ResizeObserver loop") >= 0) return' in js
