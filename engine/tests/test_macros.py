"""电脑控制操作回放（P13）：录制口径、宏库读写、二次确认与重放执行。

安全断言是本文件的重点：
- 只录「可重放动作」（鼠标/键盘/窗口/写剪贴板），截屏与读剪贴板不录；
- 只录**真执行成功**的调用（有配对 tool_result 且非 is_error）：deny、
  钩子/Mod 拦截、取消挂起的动作一律不入宏，宏不成为权限门的旁路；
- 重放没带 confirm 时**只回预览、绝不执行**；
- 电脑控制关着时拒绝重放（宏不是绕过开关的后门）；
- 重放执行面只收 MACRO_ACTIONS 四件套：macros.json 被篡改混进来的名单外
  工具步整段拒绝执行；
- 确认面与执行面等价：keyboard/clipboard 的 text 预览全文可见，前端不裁步骤；
- stop 经 ctx.aborted 传导进步内，长操作可被中途打断，不再只在步间生效；
- 单步失败即停，不把剩下的动作继续注入。
"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.core import macros as macro_lib
from skysheep.messages import Message, ToolUseBlock
from skysheep.tools import ToolContext
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


def test_extract_steps_only_keeps_executed_replayable_actions():
    """只录「真执行成功」的动作：可重放类 + 有配对 tool_result 且非 is_error。

    旧口径只看 assistant 里的 ToolUseBlock，从未执行/被拒的动作也会混进宏，
    改口径后必须配到非错误结果才入宏（改测试理由：旧用例钉死的是错误口径）。
    """
    msgs = [
        Message.assistant([
            ToolUseBlock(id="t1", name="screenshot", input={}),  # 不可重放：不录
            ToolUseBlock(id="t2", name="mouse", input={"action": "click", "x": 10, "y": 20}),
        ]),
        Message.tool_result("t2", "已执行 click @ (10, 20)"),
        Message.assistant([
            ToolUseBlock(id="t3", name="keyboard", input={"text": "hello"}),
            ToolUseBlock(id="t4", name="clipboard_read", input={}),  # 不可重放：不录
            ToolUseBlock(id="t5", name="clipboard_write", input={"text": "copied"}),
            ToolUseBlock(id="t6", name="window", input={"action": "focus", "title": "记事本"}),
        ]),
        Message.tool_result("t3", "已输入 5 个字符"),
        Message.tool_result("t4", "剪贴板内容"),
        Message.tool_result("t5", "已写入剪贴板"),
        Message.tool_result("t6", "已把「记事本」置为前台窗口"),
    ]
    steps = macro_lib.extract_steps(msgs)
    assert [s["tool"] for s in steps] == ["mouse", "keyboard", "clipboard_write", "window"]
    assert steps[0]["input"]["x"] == 10


def test_extract_steps_excludes_denied_blocked_and_pending():
    """deny / 钩子·Mod 拦截 / 取消挂起三类都不入宏：宏不是权限门的旁路。

    agent 循环对这三类都落 is_error=True 的 tool_result（deny：agent.py 的
    Decision.DENY 分支；拦截：hook / Mod deny 分支）或干脆没有结果（轮次
    取消挂起），extract_steps 凭配对结果把它们全部排除。
    """
    msgs = [
        Message.assistant([
            ToolUseBlock(id="ok1", name="mouse", input={"action": "click"}),
            ToolUseBlock(id="deny1", name="keyboard", input={"text": "被拒的输入"}),
            ToolUseBlock(id="hook1", name="clipboard_write", input={"text": "被拦截的写入"}),
            ToolUseBlock(id="hang1", name="window", input={"action": "close", "title": "挂起"}),
        ]),
        Message.tool_result("ok1", "已执行 click"),
        Message.tool_result("deny1", "User denied this operation.", is_error=True),
        Message.tool_result("hook1", "blocked by pre_tool_use hook: 规则命中", is_error=True),
        # hang1 没有配对结果：轮次被取消/崩溃，动作从未真正执行
    ]
    steps = macro_lib.extract_steps(msgs)
    assert [s["tool"] for s in steps] == ["mouse"]


def test_extract_steps_caps_at_max():
    n = macro_lib.MAX_STEPS + 20
    msgs = [
        Message.assistant([
            ToolUseBlock(id=f"t{i}", name="keyboard", input={"text": str(i)})
            for i in range(n)
        ]),
        *[Message.tool_result(f"t{i}", "ok") for i in range(n)],
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


def test_describe_step_shows_full_text():
    """确认面与执行面等价：keyboard/clipboard 的 text 全文可见，不截断不压行。

    曾经 60 字符截断 + 空白折叠：预览的省略号背后可能藏着另一段输入，重放
    却会原样注入——用户确认的是看不见的内容。
    """
    long_text = "甲" * 100
    line = macro_lib.describe_step({"tool": "keyboard", "input": {"text": long_text}})
    assert long_text in line, "超长输入必须全文可见：截断的预览等于让人确认看不见的内容"
    assert "…" not in line
    multi = macro_lib.describe_step({"tool": "keyboard", "input": {"text": "第一行\n第二行"}})
    assert "第一行\n第二行" in multi, "换行原样保留：重放时换行就是真的回车"
    spaced = macro_lib.describe_step({"tool": "clipboard_write", "input": {"text": "a  b\tc"}})
    assert "a  b\tc" in spaced, "空白折叠也会让预览失真"
    clip = macro_lib.describe_step({"tool": "clipboard_write", "input": {"text": "乙" * 80}})
    assert "乙" * 80 in clip


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


def test_replay_refuses_out_of_allowlist_step():
    """macros.json 被篡改混进名单外工具步：整个宏拒绝执行，一步都不跑。

    load_macros 只验 name/steps 结构，不验工具名（normalize_steps 只管写入路径）；
    手改文件塞进来的 run_command 等任意工具步在重放执行面必须被名单拦下——
    先验全部步、再执行任何一步，篡改过的宏要么整段被拒、要么按预览执行。
    """
    mouse = _FakeAction("mouse")
    evil = _FakeAction("run_command")
    out = asyncio.run(macro_lib.replay_steps(
        _Registry([mouse, evil]), None, [
            {"tool": "mouse", "input": {"action": "click"}},
            {"tool": "run_command", "input": {"command": "calc.exe"}},
        ],
    ))
    assert out["executed"] == 0, "名单外步存在时不得执行任何一步"
    assert "拒绝执行" in out["error"] and "run_command" in out["error"]
    assert mouse.calls == [] and evil.calls == [], "拒绝时连名单内的步也不得开跑"


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


def test_replay_stop_interrupts_step_in_progress():
    """stop 经 ctx.aborted 传导进步内：步内长操作可被中途打断，不再只步间生效。

    ctx 的接线（ToolContext(aborted=stop)）与 backend_parts/macros.py 的
    macro_replay 一致；假工具模拟 _type_text_sync 的逐字符长操作——每轮检查
    ctx.aborted，与 tools/computer.py:274-292 同构。
    """

    class _SlowTyping(Tool):
        safety = Safety.DANGEROUS
        name = "keyboard"
        description = "慢速打字（假）"
        args_model = macro_lib_args()

        def __init__(self):
            self.finished_aborted: list[bool] = []

        async def run(self, args, ctx):
            for _ in range(500):
                if ctx.aborted.is_set():
                    self.finished_aborted.append(True)
                    return "已输入 3/500 字符后被用户中止"
                await asyncio.sleep(0.01)
            self.finished_aborted.append(False)
            return "已输入 500 个字符"

    slow = _SlowTyping()
    stop = asyncio.Event()
    ctx = ToolContext(working_dir=None, aborted=stop)

    async def _main():
        async def _stop_later():
            await asyncio.sleep(0.05)
            stop.set()

        stopper = asyncio.create_task(_stop_later())
        out = await macro_lib.replay_steps(
            _Registry([slow]), ctx,
            [{"tool": "keyboard", "input": {"text": "很长的输入"}}] * 2,
            delay_ms=macro_lib.MIN_STEP_DELAY_MS, stop=stop,
        )
        await stopper
        return out

    out = asyncio.run(_main())
    assert slow.finished_aborted == [True], "步内操作必须能看到 stop 已置位（否则接线断了）"
    assert out["stopped"] is True and out["executed"] == 1, \
        "被打断的这一步计入已执行，后续一步不再开跑"
    assert "中止" in out["results"][0]["preview"]

# ---- WS 层：二次确认、开关拒绝、local_only ----


def _ws_call(ws, rid, method, params=None):
    from test_server import recv_until

    ws.send_json({"id": rid, "method": method, "params": params or {}})
    return recv_until(ws, rid)


def _append_mouse_message(client, sid):
    """在隔离库里给该会话补「Agent 点过鼠标且执行成功」的历史。

    与真实历史同构：assistant 的 ToolUseBlock 之后必然跟着配对的非错误
    tool_result（agent 循环执行完落历史）——只有调用没有结果的动作按新
    口径视为「未执行」，不录。
    """
    from skysheep.messages import Message, TextBlock, ToolUseBlock

    backend = client.app.state.backend
    msgs = [
        Message.assistant([
            TextBlock(text="我来点一下"),
            ToolUseBlock(id="t1", name="mouse", input={"action": "click", "x": 5, "y": 6}),
        ]),
        Message.tool_result("t1", "已执行 click @ (5, 6)"),
    ]
    client.portal.call(_append_async, backend, sid, msgs)


async def _append_async(backend, sid, msgs):
    for msg in msgs:
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
        assert "桌面" in preview["result"]["hint"], \
            "确认提示必须写明 window/鼠标步按重放时点桌面与坐标解析的漂移风险"

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


async def _await_macro_task(backend):
    """等后台重放任务走完（含 macro_finished 广播），测试才能收尸。"""
    task = getattr(backend, "_macro_task", None)
    if task is not None:
        await task


def test_macro_ws_replay_filters_registry_and_wires_abort(home, monkeypatch):
    """重放执行面接线：注册表只收 MACRO_ACTIONS 四件套，stop 挂进 ctx.aborted。

    假 replay_steps 捕获实参（不真注入键鼠）：注册表全量注册表的话，被篡改
    宏里的任意工具在执行面就是可调用的；ctx.aborted 不接 stop 的话，步内
    长操作打不断。
    """
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        backend.cfg.computer_control = True  # 只为过执行面检查，假 replay 不会真注入
        ws.send_json({"id": "n1", "method": "session.new", "params": {}})
        sid = recv_until(ws, "n1")["result"]["id"]
        _append_mouse_message(client, sid)
        saved = _ws_call(ws, "m1", "macro.save", {"name": "接线检查", "session_id": sid})
        assert saved["ok"], saved.get("error")

        captured: dict = {}

        async def _fake_replay(registry, ctx, steps, **kw):
            captured["registry"] = registry
            captured["ctx"] = ctx
            captured["stop"] = kw.get("stop")
            return {"executed": len(steps), "total": len(steps),
                    "stopped": False, "results": [], "error": ""}

        monkeypatch.setattr(macro_lib, "replay_steps", _fake_replay)
        r = _ws_call(ws, "m2", "macro.replay", {"name": "接线检查", "confirm": True})
        assert r["ok"], r.get("error")
        client.portal.call(_await_macro_task, backend)

        assert captured, "confirm 后必须真的进入执行面"
        names = {t.name for t in captured["registry"].all()}
        assert names <= set(macro_lib.MACRO_ACTIONS), \
            f"重放注册表只能有四件套，混进了：{names - set(macro_lib.MACRO_ACTIONS)}"
        assert captured["ctx"].aborted is captured["stop"], \
            "stop 必须挂进 ToolContext.aborted，否则步内长操作打不断"


def test_macro_ws_methods_are_local_only():
    """键鼠注入只对本机有意义：写/放/看步详情都只允许本机。"""
    from skysheep.server import app as server_app

    for name in ("macro.get", "macro.save", "macro.delete", "macro.replay", "macro.stop"):
        entry = server_app._WS_METHODS.get(name)
        assert entry is not None, name
        assert entry.local_only is True, name
    listing = server_app._WS_METHODS["macro.list"]
    assert listing.local_only is False, "清单只有名字与步数，各端可读"
