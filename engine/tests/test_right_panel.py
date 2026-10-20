"""底部终端面板（PTY 真终端：term.spawn/input/resize/close）、辅助对话（chat.aux，
支持多实例 auxN 各自独立历史）、审查（checkpoint.diff）、项目记忆读写（编码安全 /
截断明示 / 切项目重置 aux 历史）、以及界面偏好的标签持久化（right_tabs 等）。"""

from __future__ import annotations

import sys
import time

import pytest
from conftest import read_app_bundle
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


def test_chat_aux_multiple_instances_are_isolated(home):
    """辅助对话多开：不同 aux_id 各自一份独立历史，互不串上下文；
    aux.clear 只清指定的那个实例，其余实例的记忆保留。"""
    from skysheep.models.fake import FakeProvider

    prov = FakeProvider([
        [TextBlock(text="A1")], [TextBlock(text="B1")],
        [TextBlock(text="A2")], [TextBlock(text="B2")],
    ])
    with make_client(home, [], provider=prov) as client, client.websocket_connect("/ws") as ws:
        def ask(mid, text, aux_id):
            ws.send_json({"id": mid, "method": "chat.aux",
                          "params": {"text": text, "aux_id": aux_id}})
            return recv_until(ws, mid)

        assert ask("a1", "问 A 一", "aux1")["ok"]
        assert ask("b1", "问 B 一", "aux2")["ok"]
        # aux2 的第二问不该看到 aux1 的问答
        assert ask("b2", "问 B 二", "aux2")["ok"]
        second_b = prov.calls[2]
        assert [m.role for m in second_b] == ["system", "user", "assistant", "user"]
        assert "问 B 一" in second_b[1].text
        assert all("问 A 一" not in m.text for m in second_b if m.role != "system")

        backend = client.app.state.backend
        assert set(backend.aux_histories) == {"aux1", "aux2"}

        # 清 aux1 不影响 aux2
        ws.send_json({"id": "c1", "method": "aux.clear", "params": {"aux_id": "aux1"}})
        assert recv_until(ws, "c1")["result"]["cleared"] is True
        assert "aux1" not in backend.aux_histories
        roles_b = [m.role for m in backend.aux_histories["aux2"]]
        assert roles_b == ["system", "user", "assistant", "user", "assistant"]


def test_chat_aux_requires_provider(home):
    # 不注入 provider 工厂 → provider 为 None → 给出可读错误
    from fastapi.testclient import TestClient

    from skysheep.server import create_app

    app = create_app(working_dir=home / "proj")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "chat.aux", "params": {"text": "hi"}})
        frame = recv_until(ws, "a1")
        assert not frame["ok"] and "模型服务未配置" in frame["error"]


# ---------- 辅助对话模型（aux.model.set / aux.model.get） ----------

def call(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": method, "params": params or {}})
        return recv_until(ws, "m1")


def test_aux_model_set_get_clear(home):
    """set 校验服务名与模型登记；get 回包带主模型标注；空名清除。"""
    import json

    frame = call(home, "aux.model.set", {"name": "不存在", "model": "m"})
    assert not frame["ok"]  # 未知服务拒绝

    frame = call(home, "aux.model.set", {"name": "", "model": "m"})
    assert not frame["ok"]  # 清除时不能只留模型名

    # provider_factory 注入绕过 config，fake 服务不在 cfg.providers 里；
    # 用真实预设名验证（models 未登记 → 任意模型名放行），与 default_model 同口径
    frame = call(home, "aux.model.set", {"name": "deepseek", "model": "deepseek-chat"})
    assert frame["ok"] and frame["result"]["provider"] == "deepseek"
    assert frame["result"]["label"] == "deepseek / deepseek-chat"
    prefs = json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))
    assert prefs["aux_model_provider"] == "deepseek"

    frame = call(home, "aux.model.get")
    assert frame["ok"] and frame["result"]["provider"] == "deepseek"
    assert frame["result"]["model"] == "deepseek-chat"
    assert "main_provider" in frame["result"]  # 入口徽章画主对话服务 logo 用的

    frame = call(home, "aux.model.set", {"name": ""})
    assert frame["ok"] and frame["result"]["provider"] == ""
    prefs = json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))
    assert "aux_model_provider" not in prefs


def test_aux_model_chat_uses_detached_provider(home):
    """设了辅助对话专用模型后 chat.aux 照常流式，且不碰主对话的 provider 状态。"""
    from skysheep.models.fake import FakeProvider

    prov = FakeProvider([[TextBlock(text="专用回答")]])
    with make_client(home, [], provider=prov) as client, client.websocket_connect("/ws") as ws:
        def rpc(mid, method, params=None, events=None):
            ws.send_json({"id": mid, "method": method, "params": params or {}})
            return recv_until(ws, mid, events)

        before = rpc("g1", "aux.model.get")
        assert before["ok"]
        assert rpc("s1", "aux.model.set", {"name": "deepseek", "model": "deepseek-chat"})["ok"]

        events = []
        frame = rpc("a1", "chat.aux", {"text": "侧栏问题"}, events)
        assert frame["ok"] and frame["result"]["text"] == "专用回答"
        deltas = "".join(e["data"]["text"] for e in events if e["event"] == "aux_delta")
        assert "专用回答" in deltas
        assert len(prov.calls) == 1  # 专用 provider（override 注入的同一实例）接到了这次调用

        # 关键回归点：辅助对话换模型不得碰主对话的状态面（provider_name 等不变）
        after = rpc("g2", "aux.model.get")
        assert after["ok"]
        assert after["result"]["main_label"] == before["result"]["main_label"]
        assert after["result"]["provider"] == "deepseek"


def test_aux_model_wired(home):
    """前端接线：发送钮上方的徽章入口、弹出菜单容器、装载与切换调用都在。

    辅助对话自 2.6.2 起支持多开：每个 auxN 的页面由 createAuxPage 动态创建，
    徽章/输入框/发送钮从固定 id 改为类名（.aux-model-chip 等），因此这里校验
    JS 源码里的模板结构，不再依赖 index.html 里的静态节点。
    """
    from skysheep.server.app import STATIC_DIR

    js = read_app_bundle()
    assert "aux.model.get" in js and "aux.model.set" in js
    assert "refreshAuxModel" in js and "pickAuxModel" in js
    assert "providerAvatar(" in js  # 闭合态只显品牌 logo
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="aux-model-menu"' in html  # 菜单容器仍是全局单例
    # 徽章与发送按钮同侧上下排列：同在 aux-row 右侧的 aux-side 纵列里，
    # 徽章在上、发送在下（DOM 顺序 textarea → 徽章 → 发送），由 createAuxPage 生成
    assert "function createAuxPage(" in js
    assert 'class="aux-side"' in js
    i_input = js.index('class="aux-input"')
    i_chip = js.index('class="aux-model-chip"')
    i_send = js.index('class="rp-mini primary aux-send-btn"')
    assert i_input < i_chip < i_send


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


def test_aux_multi_instance_prefs_roundtrip(home):
    """辅助对话多开实例的标签持久化：auxN 形式的 id 合法可存取；aux_titles
    自定义标题随偏好往返；非 auxN 的伪造 id 与空标题被拒。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({
            "id": "u1", "method": "ui.save",
            "params": {"prefs": {
                "right_tabs": ["aux1", "aux2", "files", "auxBogus", "aux"],
                "right_active": "aux2",
                "aux_titles": {"aux1": "代码问题", "aux2": "   ", "files": "越界"},
            }},
        })
        frame = recv_until(ws, "u1")
        prefs = frame["result"]["prefs"]
        # aux1/aux2 收下；auxBogus 不是 auxN 形式；files 不是辅助对话实例
        assert prefs["right_tabs"] == ["aux1", "aux2", "files", "aux"]
        assert prefs["right_active"] == "aux2"
        # 空白标题与非 auxN 键都不收
        assert prefs["aux_titles"] == {"aux1": "代码问题"}

        ws.send_json({"id": "u2", "method": "ui.get"})
        stored = recv_until(ws, "u2")["result"]["prefs"]
        assert stored["aux_titles"] == {"aux1": "代码问题"}
        assert stored["right_tabs"] == ["aux1", "aux2", "files", "aux"]

        # 传 null 删键（= 全部回到默认名）
        ws.send_json({"id": "u3", "method": "ui.save", "params": {"prefs": {"aux_titles": None}}})
        assert "aux_titles" not in recv_until(ws, "u3")["result"]["prefs"]


def test_rp_tabs_compress_and_overflow_menu(home):
    """标签条压缩呈现：先均分收缩、藏图标留字；挤到看不清时出现行尾「»」菜单。

    标签**不横向滚动**（来回拖找标签成本高）：容器 overflow 收住，标签永远
    均分压缩、文字截断加省略号；不足 92px/个藏图标只留字；不足 64px/个时
    文字已只能显示两三个字，这时行尾出现「»」按钮，点开列出全部标签直接跳。
    档位由 JS 渲染后按均分宽度判定。
    """
    from skysheep.server.app import STATIC_DIR

    js = read_app_bundle()
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    # CSS：容器不横滚（overflow:hidden）；标签可压缩、文字截断
    tabs_block = css.split("#rp-tabs {")[1].split("}")[0]
    assert "overflow-x: auto" not in tabs_block, "标签条不再横向滚动"
    assert "overflow: hidden" in tabs_block
    assert "#rp-tabs::-webkit-scrollbar" not in css, "滚动条样式随之删除"
    tab_block = css.split(".rp-tab {")[1].split("}")[0]
    assert "flex: 1 1 0" in tab_block and "min-width: 0" in tab_block
    span_rule = css.split(".rp-tab > span:not(.rp-x) {")[1].split("}")[0]
    assert "text-overflow: ellipsis" in span_rule
    assert "#rp-tabs.rp-tight" in css, "不足 92px/个时藏图标留字"
    assert "rp-scroll" not in css, "横向滚动档已删除"
    assert "rp-cramped" not in css and "rp-icon" not in css, "紧凑档/图标档已废除"
    # 「»」全部标签按钮：平时不显示，挤到看不清时（rp-has-more）出现
    more_rule = css.split(".rp-more {")[1].split("}")[0]
    assert "display: none" in more_rule
    assert "#rp-tabs.rp-has-more .rp-more { display: inline-flex; }" in css
    # JS：档位判定抽成 applyRpTabDensity，渲染与容器宽度变化（ResizeObserver）共用
    assert "function applyRpTabDensity(" in js
    assert 'classList.toggle("rp-tight", key === "tight")' in js
    assert 'classList.toggle("rp-has-more", key === "more")' in js
    assert '"scroll"' not in js.split("function applyRpTabDensity(")[1].split("}")[0]
    assert "iconOnly" not in js and '"cramped"' not in js, "图标档/紧凑档判定已删"
    assert "ResizeObserver" in js, "拖面板宽度后档位要即时跟上"
    # 溢出菜单列出全部标签、直接激活
    assert "function showRpOverflowMenu(" in js
    assert "activateRightTab(id)" in js
    # 「＋」钮在标签栏最左
    assert "rpTabs.prepend(addBtn)" in js, "＋ 要插在所有标签之前（行首）"
    # RO 回调必须 rAF 推迟 + 档位无变化不写 DOM：回调内同步改布局会刷
    # 「ResizeObserver loop completed」告警（真循环的根因在各自 RO 回调里修；
    # 曾兜底的调试错误陷阱 __errdump 已按评审移除，这里只锁根因侧的修法）
    assert "requestAnimationFrame(() => applyRpTabDensity())" in js
    assert "if (key === _rpDensityKey) return" in js
    # 调试错误陷阱不得回归（红屏浮层随 2.1.0 误发布过一次）
    assert "__errdump" not in js and "ERR_TRAP" not in js


def test_chat_aux_remote_is_stateless(home, monkeypatch):
    """审查 P1-3 收口：远程调用的辅助对话必须无状态。

    共享 aux_histories 是本机侧栏的面板语义——远端借「重复上面的内容」类提问
    能套出本机用户问过什么，远端的提问也不该写进本机面板。local=False 时
    用一次性历史：模型只看到 system + 本条消息，共享历史零读写。
    """
    from skysheep.models.fake import FakeProvider
    from skysheep.server import app as server_app

    prov = FakeProvider([[TextBlock(text="远端答案")], [TextBlock(text="本地答案")]])
    monkeypatch.setattr(server_app, "_client_is_local", lambda ws: False)
    with make_client(home, [], provider=prov) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "chat.aux", "params": {"text": "远端问题"}})
        frame = recv_until(ws, "a1")
        assert frame["ok"] and frame["result"]["text"] == "远端答案"
        assert frame["result"].get("stateless") is True
        # 模型只看到 system + 本条消息：读不到任何此前内容
        assert [m.role for m in prov.calls[0]] == ["system", "user"]
        # 共享历史零读写
        backend = client.app.state.backend
        assert backend.aux_histories == {}

    # 本机调用语义不变：仍走共享历史（先恢复 _client_is_local 的远端模拟）
    monkeypatch.undo()
    with make_client(home, [], provider=prov) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a2", "method": "chat.aux", "params": {"text": "本地问题"}})
        assert recv_until(ws, "a2")["ok"]
        backend = client.app.state.backend
        assert [m.role for m in backend.aux_histories["aux1"]] == ["system", "user", "assistant"]
