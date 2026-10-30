"""电脑控制操作回放（P13）：录制口径、宏库读写、二次确认与重放执行。

安全断言是本文件的重点：
- 只录「可重放动作」（鼠标/键盘/窗口/写剪贴板），截屏与读剪贴板不录；
- 重放没带 confirm 时**只回预览、绝不执行**；
- 电脑控制关着时拒绝重放（宏不是绕过开关的后门）；
- 单步失败即停，不把剩下的动作继续注入。
"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.core import macros as macro_lib
from skysheep.messages import Message, ToolUseBlock
from skysheep.tools.base import Safety, Tool


class _FakeAction(Tool):
    safety = Safety.DANGEROUS

    def __init__(self, name: str, *, fail: bool = False):
        self.name = name
        self.description = "假动作"
        self.args_model = macro_lib_args()
        self.fail = fail
        self.calls: list[dict] = []

    async def run(self, args, ctx):
        self.calls.append(dict(args))
        if self.fail:
            from skysheep.tools.base import ToolError

            raise ToolError("注入失败（假）")
        return "ok"


def macro_lib_args():
    from pydantic import BaseModel, ConfigDict

    class _Args(BaseModel):
        model_config = ConfigDict(extra="allow")

    return _Args


class _Registry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def get(self, name):
        return self._tools.get(name)


# ---- 录制口径 ----


def test_extract_steps_only_keeps_replayable_actions():
    msgs = [
        Message.assistant([
            ToolUseBlock(id="t1", name="screenshot", input={}),
            ToolUseBlock(id="t2", name="mouse", input={"action": "click", "x": 10, "y": 20}),
        ]),
        Message.assistant([
            ToolUseBlock(id="t3", name="keyboard", input={"text": "hello"}),
            ToolUseBlock(id="t4", name="clipboard_read", input={}),
            ToolUseBlock(id="t5", name="clipboard_write", input={"text": "copied"}),
            ToolUseBlock(id="t6", name="window", input={"action": "focus", "title": "记事本"}),
        ]),
    ]
    steps = macro_lib.extract_steps(msgs)
    assert [s["tool"] for s in steps] == ["mouse", "keyboard", "clipboard_write", "window"]
    assert steps[0]["input"]["x"] == 10


def test_extract_steps_caps_at_max():
    msgs = [
        Message.assistant([
            ToolUseBlock(id=f"t{i}", name="keyboard", input={"text": str(i)})
            for i in range(macro_lib.MAX_STEPS + 20)
        ])
    ]
    assert len(macro_lib.extract_steps(msgs)) == macro_lib.MAX_STEPS


def test_normalize_steps_rejects_unknown_tools_and_bad_input():
    steps = macro_lib.normalize_steps([
        {"tool": "screenshot", "input": {}},        # 不可重放：丢
        {"tool": "mouse", "input": "not-a-dict"},   # 参数不是对象：丢
        {"tool": "mouse", "input": {"action": "click"}},
        "junk",
    ])
    assert steps == [{"tool": "mouse", "input": {"action": "click"}}]


# ---- 宏库读写 ----


def test_macro_store_roundtrip_and_overwrite(home):
    assert macro_lib.load_macros() == {}
    macro_lib.put_macro("导出报表", [{"tool": "mouse", "input": {"action": "click"}}], session_id="s1")
    items = macro_lib.load_macros()
    assert list(items) == ["导出报表"]
    assert items["导出报表"]["session_id"] == "s1"
    # 同名覆盖（用户重录同一段操作是常态），不产生第二条
    macro_lib.put_macro("导出报表", [
        {"tool": "keyboard", "input": {"text": "a"}},
        {"tool": "mouse", "input": {"action": "click"}},
    ])
    items = macro_lib.load_macros()
    assert list(items) == ["导出报表"] and len(items["导出报表"]["steps"]) == 2
    assert macro_lib.delete_macro("导出报表") is True
    assert macro_lib.load_macros() == {}
    assert macro_lib.delete_macro("导出报表") is False


def test_macro_store_rejects_empty(home):
    with pytest.raises(ValueError):
        macro_lib.put_macro("空的", [{"tool": "screenshot", "input": {}}])
    with pytest.raises(ValueError):
        macro_lib.put_macro("  ", [{"tool": "mouse", "input": {"action": "click"}}])


def test_macro_store_tolerates_garbage_file(home):
    path = macro_lib.macros_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{不是 json", encoding="utf-8")
    assert macro_lib.load_macros() == {}
    path.write_text('{"macros": [{"name": "x", "steps": []}, "junk"]}', encoding="utf-8")
    assert macro_lib.load_macros() == {}, "空步/畸形条目一律不收"


def test_describe_step_is_human_readable():
    assert macro_lib.describe_step({"tool": "mouse", "input": {"action": "click", "x": 3, "y": 4}}) \
        == "鼠标 click (3,4)"
    assert "输入「hi」" in macro_lib.describe_step({"tool": "keyboard", "input": {"text": "hi"}})
    assert "窗口 focus" in macro_lib.describe_step(
        {"tool": "window", "input": {"action": "focus", "title": "记事本"}})


# ---- 重放执行 ----


def test_replay_runs_steps_in_order():
    mouse = _FakeAction("mouse")
    keyboard = _FakeAction("keyboard")
    seen: list[dict] = []
    steps = [
        {"tool": "mouse", "input": {"action": "click", "x": 1, "y": 2}},
        {"tool": "keyboard", "input": {"text": "abc"}},
    ]
    out = asyncio.run(macro_lib.replay_steps(
        _Registry([mouse, keyboard]), None, steps,
        delay_ms=macro_lib.MIN_STEP_DELAY_MS, on_step=seen.append,
    ))
    assert out["executed"] == 2 and out["stopped"] is False and out["error"] == ""
    assert [c["action"] for c in mouse.calls] == ["click"]
    assert keyboard.calls == [{"text": "abc"}]
    assert [r["index"] for r in seen] == [1, 2]


def test_replay_stops_when_tool_missing():
    """电脑控制没打开（工具未注册）：立刻停，报可读原因，不跳过继续。"""
    out = asyncio.run(macro_lib.replay_steps(
        _Registry([]), None,
        [{"tool": "mouse", "input": {"action": "click"}}],
    ))
    assert out["executed"] == 0
    assert "电脑控制" in out["error"]


def test_replay_stops_on_step_failure():
    mouse = _FakeAction("mouse", fail=True)
    keyboard = _FakeAction("keyboard")
    out = asyncio.run(macro_lib.replay_steps(
        _Registry([mouse, keyboard]), None, [
            {"tool": "mouse", "input": {"action": "click"}},
            {"tool": "keyboard", "input": {"text": "x"}},
        ],
    ))
    assert out["executed"] == 1
    assert "第 1 步执行失败" in out["error"]
    assert keyboard.calls == [], "前一步失败后不得继续注入后面的动作"


def test_replay_honours_stop_event():
    stop = asyncio.Event()
    mouse = _FakeAction("mouse")

    def on_step(row):
        stop.set()  # 第一步完成就请求停止

    out = asyncio.run(macro_lib.replay_steps(
        _Registry([mouse]), None,
        [{"tool": "mouse", "input": {"action": "click"}}] * 4,
        delay_ms=macro_lib.MIN_STEP_DELAY_MS, stop=stop, on_step=on_step,
    ))
    assert out["stopped"] is True and out["executed"] == 1

# ---- WS 层：二次确认、开关拒绝、local_only ----


def _ws_call(ws, rid, method, params=None):
    from test_server import recv_until

    ws.send_json({"id": rid, "method": method, "params": params or {}})
    return recv_until(ws, rid)


def _append_mouse_message(client, sid):
    """在隔离库里给该会话补一条「Agent 点过鼠标」的助手消息。"""
    from skysheep.messages import Message, TextBlock, ToolUseBlock

    backend = client.app.state.backend
    msg = Message.assistant([
        TextBlock(text="我来点一下"),
        ToolUseBlock(id="t1", name="mouse", input={"action": "click", "x": 5, "y": 6}),
    ])
    client.portal.call(_append_async, backend, sid, msg)


async def _append_async(backend, sid, msg):
    await backend.store.append_message(sid, msg)


def _open(home):
    """建 client 与一条会话，返回 (client 上下文, ws 上下文, sid)。"""
    from test_server import make_client

    client = make_client(home, [])
    return client


def test_macro_ws_needs_confirm_and_refuses_when_computer_off(home):
    """重放两次调用：第一次只回预览（绝不执行），确认后仍因开关关着被拒。"""
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new", "params": {}})
        sid = recv_until(ws, "n1")["result"]["id"]
        _append_mouse_message(client, sid)

        saved = _ws_call(ws, "m1", "macro.save", {"name": "点击五号位", "session_id": sid})
        assert saved["ok"], saved.get("error")
        assert [m["name"] for m in saved["result"]["macros"]] == ["点击五号位"]
        assert saved["result"]["macros"][0]["steps"] == 1

        preview = _ws_call(ws, "m2", "macro.replay", {"name": "点击五号位"})
        assert preview["ok"], preview.get("error")
        assert preview["result"]["needs_confirm"] is True
        assert preview["result"]["steps"] == 1
        assert "鼠标 click (5,6)" in preview["result"]["preview"][0]

        # 带 confirm 但电脑控制默认关：拒绝，且给出可读原因
        forced = _ws_call(ws, "m3", "macro.replay", {"name": "点击五号位", "confirm": True})
        assert forced["ok"] is False
        assert "电脑控制" in forced["error"]

        listing = _ws_call(ws, "m4", "macro.list")
        assert listing["result"]["computer_control"] is False

        gone = _ws_call(ws, "m5", "macro.delete", {"name": "点击五号位"})
        assert gone["ok"] and gone["result"]["macros"] == []
        missing = _ws_call(ws, "m6", "macro.delete", {"name": "点击五号位"})
        assert missing["ok"] is False


def test_macro_ws_save_requires_actions(home):
    """会话里没有可录制动作：报错，不写空宏。"""
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new", "params": {}})
        sid = recv_until(ws, "n1")["result"]["id"]
        r = _ws_call(ws, "m1", "macro.save", {"name": "空的", "session_id": sid})
        assert r["ok"] is False and "没有可录制" in r["error"]


def test_macro_ws_methods_are_local_only():
    """键鼠注入只对本机有意义：写/放/看步详情都只允许本机。"""
    from skysheep.server import app as server_app

    for name in ("macro.get", "macro.save", "macro.delete", "macro.replay", "macro.stop"):
        entry = server_app._WS_METHODS.get(name)
        assert entry is not None, name
        assert entry.local_only is True, name
    listing = server_app._WS_METHODS["macro.list"]
    assert listing.local_only is False, "清单只有名字与步数，各端可读"
