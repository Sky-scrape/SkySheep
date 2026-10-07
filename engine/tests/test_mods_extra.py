"""Mods 扩展（实验性）补充测试：沙箱边界、启停接线、挂点次序与 WS 方法缺口。

test_mods.py / test_mods_security.py / test_server.py 已覆盖清单校验、动作闭集、
extra_confirm 三道结构锁与主要 WS 方法；本文件补齐尚无用例的面：

- 沙箱边界：quickjs 沙箱内没有文件/网络/进程能力面（宿主零注入），eval/Function
  也摸不到宿主绑定；内存超限被容器化成 handler 错误；Mod 之间互不可见；入口超限拒载。
- 启停接线：backend 启动无 Mod 时 mods=None、门不挂收紧查询；toggle 把
  extra_confirm 接到真实权限门上；mods.delete 连带清目录/状态/config 条目。
- 挂点次序：hooks 拦截时同一次调用的 toolPre 不再执行；mods 有 toolPre 时
  只读并发批断批（逐个串行过 Mod）；ModUI 每轮 50 条上限 + 单次溢出 Notice。
- WS 方法：mods.install_official（bundled 缺席时回落仓库）、mods_updated 广播。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeProvider
from test_server import make_client, recv_until

from skysheep.core.agent import Agent
from skysheep.core.hooks import HookRule, HookRunner
from skysheep.core.mods import (
    ModManager,
    load_mod_state,
    mods_root,
    mods_state_root,
)
from skysheep.events import ModUI, NoticeEvent, ToolCallFinished, ToolCallStarted
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate, WhitelistRule
from skysheep.tools import ToolRegistry, default_tools

# ---- 夹具与助手 ----


def _write_mod(
    mod_id: str, main_js: str, manifest: dict | None = None, *,
    filename: str = "mod.json",
) -> Path:
    """把一个最小 Mod 写进隔离 home 的 Mods 根目录。"""
    mod_dir = mods_root() / mod_id
    mod_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"id": mod_id, "name": mod_id, "version": "1.0.0",
                "hooks": [], "permissions": "observe", **(manifest or {})}
    manifest["id"] = mod_id
    (mod_dir / filename).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")
    return mod_dir


def make_agent(provider, tmp_path, mods=None, gate=None, hooks=None):
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=gate or PermissionGate(working_dir=tmp_path),
        working_dir=tmp_path,
        mods=mods,
        hooks=hooks,
    )


async def collect(agent, text, auto_respond="allow_once"):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request" and auto_respond:
            agent.respond_permission(ev.request_id, auto_respond)
    return events


# ---- 沙箱边界：Mod 拿不到任意文件/网络/进程能力 ----

# 沙箱内逐个 typeof 探测的能力面名字：宿主注入为零，这些都只能是 undefined
_IO_GLOBAL_NAMES = (
    "fetch", "XMLHttpRequest", "require", "readFile", "writeFile", "process",
    "print", "console", "setTimeout", "setInterval", "Worker", "WebSocket",
    "importScripts", "crypto", "localStorage", "python",
)


def _probe_globals_js() -> str:
    checks = ", ".join(
        f"{name}: typeof globalThis['{name}']" for name in _IO_GLOBAL_NAMES
    )
    return (
        "export default { toolPre(p){"
        f" sky.state.globals = {{{checks}}};"
        " sky.state.skyKeys = Object.keys(sky).sort().join(',');"
        " return {} } }"
    )


async def test_sandbox_has_no_file_network_process_globals(home):
    """沙箱无宿主注入：文件/网络/进程面的全局全部不存在，唯一注入面是 sky。"""
    _write_mod("probe-mod", _probe_globals_js(), {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["probe-mod"])
    r = await mgr.on_tool_pre("write_file", {"path": "a"}, "s", 0)
    assert r["errors"] == []
    probe = load_mod_state("probe-mod")["s"]
    for name in _IO_GLOBAL_NAMES:
        assert probe["globals"][name] == "undefined", (
            f"沙箱里不该有 {name}（宿主零注入）"
        )
    # 唯一注入面：sky 只暴露 state（引擎代存）与 now（时钟），没有 IO 方法
    assert probe["skyKeys"] == "now,state"
    mgr.close()


async def test_dynamic_eval_cannot_reach_host_bindings(home):
    """eval / new Function 存在，但动态求值同样摸不到宿主 IO 绑定。"""
    _write_mod("eval-mod", (
        "export default { toolPre(p){"
        " sky.state.evalRequire = eval('typeof require');"
        " sky.state.funcReadFile = new Function('return typeof readFile')();"
        " try { eval('readFile(\"/etc/passwd\")'); sky.state.read = 'called'; }"
        " catch (e) { sky.state.read = 'blocked: ' + e; }"
        " return {} } }"
    ), {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["eval-mod"])
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["errors"] == []
    out = load_mod_state("eval-mod")["s"]
    assert out["evalRequire"] == "undefined"
    assert out["funcReadFile"] == "undefined"
    assert out["read"] != "called"
    mgr.close()


async def test_memory_limit_contained_as_handler_error(home):
    """内存超限（16MB 上限）被容器化成 handler 错误：结果丢弃、管理器仍可用。"""
    _write_mod("mem-mod",
               'export default { toolPre(p){ var s = "x".repeat(64*1024*1024);'
               " return {note: String(s.length)} } }",
               {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["mem-mod"])
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["note"] == "" and r["deny"] is None  # 结果已丢弃
    assert any("out of memory" in e for e in r["errors"])
    # 沙箱炸掉不连坐：管理器与该 Mod 都还活着（不是自动停用路径）
    assert not mgr.mods["mem-mod"].auto_deactivated
    r2 = await mgr.on_tool_pre("a", {}, "s", 0)
    assert any("out of memory" in e for e in r2["errors"])
    mgr.close()


async def test_mods_are_isolated_from_each_other(home):
    """每 Mod 独立 runtime：A 写进 globalThis 的东西 B 看不见，状态文件也分立。"""
    _write_mod("mod-a",
               'export default { toolPre(p){ globalThis.__leak__ = "a-secret";'
               ' sky.state.mark = "from-a"; return {} } }',
               {"hooks": ["tool_pre"]})
    _write_mod("mod-b",
               "export default { toolPre(p){ sky.state.sees ="
               " typeof globalThis.__leak__; return {} } }",
               {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["mod-a", "mod-b"])
    await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert load_mod_state("mod-b")["s"]["sees"] == "undefined"
    assert load_mod_state("mod-a")["s"]["mark"] == "from-a"
    mgr.close()


async def test_oversized_entry_rejected_at_load(home):
    """main.js 超 128KB：装载拒绝（静态审查面有硬上限），错误进设置页可见。"""
    big_js = "var pad = '" + "a" * (200 * 1024) + "';\nexport default {}"
    _write_mod("big-mod", big_js, {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["big-mod"])
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert "128KB" in mgr.mods["big-mod"].load_error
    assert not r["note"] and not r["ui"]
    mgr.close()


# ---- 启停与 backend 接线 ----


def test_backend_without_mods_has_no_manager_and_gate_unwired(home):
    """无已装 Mod：backend.mods 为 None（Agent 侧零开销直通）、门不挂收紧查询。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        assert backend.mods is None
        assert backend.gate is not None
        assert backend.gate.extra_confirm is None
        # 管理面照常可用：空清单 + runtime 诊断字段
        ws.send_json({"id": "m1", "method": "mods.list"})
        r = recv_until(ws, "m1")
        assert r["ok"] and r["result"]["mods"] == []
        assert "runtime_available" in r["result"]


def test_toggle_and_delete_wire_gate_extra_confirm(home):
    """toggle 把 extra_confirm 接到真实权限门；delete 连带清目录/状态/config。"""
    _write_mod("wire-mod", "export default {}",
               {"permissions": "tighten",
                "declarative": {"deny_tools": [],
                                "require_confirm_tools": ["run_command"]}})
    _write_mod("state-mod",
               "export default { toolPre(p){ sky.state.n = (sky.state.n || 0) + 1;"
               " return {} } }",
               {"hooks": ["tool_pre"]})
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        ws.send_json({"id": "t0", "method": "mods.set_enabled",
                      "params": {"enabled": True}})
        assert recv_until(ws, "t0")["ok"]
        for mid in ("wire-mod", "state-mod"):
            ws.send_json({"id": f"t-{mid}", "method": "mods.toggle",
                          "params": {"id": mid, "enabled": True}})
            assert recv_until(ws, f"t-{mid}")["ok"]

        # 收紧查询已接到当前权限门上（切项目换门由 _bind_project 兜住另一半）
        assert backend.mods is not None
        assert backend.gate.extra_confirm == backend.mods.extra_confirm
        # 门内生效：整工具白名单本会放行 run_command，收紧声明压回逐次确认
        backend.gate.session_rules.append(
            WhitelistRule(tool="run_command", kind="always"))
        tool = ToolRegistry(default_tools()).get("run_command")
        pending = asyncio.run(backend.gate.authorize(tool, {"command": "echo hi"}))
        assert pending is not None, "declarative 收紧必须压过白名单放行"

        # 产生一个 Mod 状态文件，随后 delete 必须连带清掉
        asyncio.run(backend.mods.on_tool_pre("write_file", {}, "sess-1", 0))
        assert (mods_state_root() / "state-mod.json").is_file()

        ws.send_json({"id": "d1", "method": "mods.delete",
                      "params": {"id": "state-mod"}})
        assert recv_until(ws, "d1")["ok"]
        assert not (mods_root() / "state-mod").exists()
        assert not (mods_state_root() / "state-mod.json").exists()

        ws.send_json({"id": "d2", "method": "mods.delete",
                      "params": {"id": "wire-mod"}})
        assert recv_until(ws, "d2")["ok"]
        # 最后一个 Mod 删掉：管理器归 None、门的收紧查询摘除
        assert backend.mods is None
        assert backend.gate.extra_confirm is None

        ws.send_json({"id": "d3", "method": "mods.delete",
                      "params": {"id": "ghost"}})
        assert recv_until(ws, "d3")["ok"] is False


async def test_disabled_manager_dispatch_is_noop(home):
    """总开关关闭（产品默认）：所有挂点空转，无 note / ui / deny。"""
    _write_mod("off-mod", "export default { toolPre(p){ return {note: 'x'} } }",
               {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=False, enabled_ids=["off-mod"])
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["note"] == "" and r["ui"] == [] and r["deny"] is None
    r2 = await mgr.on_permission_request("write_file", {}, "write", "d", "s")
    assert not r2["denied"] and r2["note"] == "" and r2["ui"] == []
    mgr.close()


def test_manager_close_shuts_down_sandbox_runtime(home):
    """close() 收掉每个 Mod 的沙箱宿主：runtime 置空、executor 进入 shutdown。"""
    _write_mod("close-mod", "export default {}", {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["close-mod"])
    mod = mgr.mods["close-mod"]
    asyncio.run(mod._ensure_probed())
    executor = mod._runtime._executor
    mgr.close()
    assert mod._runtime is None
    assert executor._shutdown


def test_reload_mods_closes_old_manager_runtime(home):
    """_reload_mods 换新前 close 旧 manager：启停/安装/删除/总开关/切项目
    不再泄漏 per-Mod 沙箱 executor 线程（非 daemon，不回收会攒线程）。"""
    _write_mod("leak-mod", "export default {}")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        ws.send_json({"id": "e0", "method": "mods.set_enabled",
                      "params": {"enabled": True}})
        assert recv_until(ws, "e0")["ok"]
        ws.send_json({"id": "e1", "method": "mods.toggle",
                      "params": {"id": "leak-mod", "enabled": True}})
        assert recv_until(ws, "e1")["ok"]
        old = backend.mods
        assert old is not None
        # 触发懒探测：建出 per-Mod 沙箱 executor（与实跑首次分发同路径）
        asyncio.run(old.on_iteration_start("s", 1))
        executor = old.mods["leak-mod"]._runtime._executor
        assert not executor._shutdown
        # 再来一次热重载（同款 toggle 路径）：旧 manager 必须被回收
        ws.send_json({"id": "e2", "method": "mods.toggle",
                      "params": {"id": "leak-mod", "enabled": False}})
        assert recv_until(ws, "e2")["ok"]
        assert backend.mods is not old
        assert executor._shutdown
        assert old.mods["leak-mod"]._runtime is None


def test_reload_mods_and_hooks_reach_team_member_agents(home):
    """Mods / hooks 热更同步推给团队成员 Agent：成员不在 _for_each_agent
    （全局刷新面，成员系统词独立），拦截手段不跟上就会对队员悄悄失效。"""
    with make_client(home, []) as client:
        backend = client.app.state.backend
        member = make_agent(FakeProvider([[TextBlock(text="x")]]), home / "proj")
        backend._teams["s1"] = SimpleNamespace(agents=lambda: [member])
        try:
            member.mods = SimpleNamespace()  # 旧实例形状
            member.hooks = SimpleNamespace()
            backend._reload_mods()
            backend._reload_hooks()
            assert member.mods is backend.mods
            assert member.hooks is backend.hooks
        finally:
            backend._teams.pop("s1", None)


# ---- 挂点次序：与 hooks 的先后、只读批断批、ModUI 预算 ----


async def test_hook_block_prevents_mod_tool_pre(home, monkeypatch):
    """hooks 已拦截时同一次调用的 toolPre 不再执行（省一次沙箱调用）。"""
    _write_mod("order-mod",
               "export default { toolPre(p){ sky.state.seen ="
               " (sky.state.seen || 0) + 1; return {} } }",
               {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["order-mod"])
    runner = HookRunner(pre_rules=[HookRule(match="write_file", command="echo")])

    async def fake_exec(rule, phase, tool_name, input_dict, session_id=""):
        return 2, "", "hook-says-block", 0  # 退出码 2 = 阻止

    monkeypatch.setattr(runner, "_exec", fake_exec)
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file",
                      input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr, hooks=runner)
    events = await collect(agent, "write a.txt")
    finished = [e for e in events if isinstance(e, ToolCallFinished)][0]
    assert "blocked by hook" in finished.preview
    # Mod 的 toolPre 没跑：既无 ModUI 事件，状态里也没有 seen 计数
    assert not [e for e in events if isinstance(e, ModUI)]
    assert "seen" not in load_mod_state("order-mod").get("", {})
    mgr.close()


async def test_tool_pre_breaks_readonly_batch_to_serial(home):
    """mods 有 toolPre 时只读并发批断批：逐个串行，每次调用都被 Mod 观察。"""
    for name in ("a.txt", "b.txt"):
        (home / "proj" / name).write_text("x", encoding="utf-8")
    _write_mod("serial-mod",
               "export default { toolPre(p){ sky.state[p.tool] ="
               " (sky.state[p.tool] || 0) + 1; return {} } }",
               {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["serial-mod"])
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"}),
         ToolUseBlock(id="t2", name="read_file", input={"path": "b.txt"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "read files")

    # 断批的直接证据：事件按「启动→完成→启动→完成」交错（批处理是两个 Started 连发）
    order = [(e.tool_call_id, type(e).__name__)
             for e in events
             if isinstance(e, (ToolCallStarted, ToolCallFinished))]
    assert order == [("t1", "ToolCallStarted"), ("t1", "ToolCallFinished"),
                     ("t2", "ToolCallStarted"), ("t2", "ToolCallFinished")]
    # 两个 READONLY 成员都被 toolPre 逐个观察到
    assert load_mod_state("serial-mod")[""]["read_file"] == 2
    mgr.close()


async def test_has_tool_pre_matches_dispatch_for_undeclared(home):
    """has_tool_pre 与 dispatch_hooks 同口径：清单未声明 hooks（CC 兼容面）时
    按实现集判断——分发改用 detected 集，断批判定漏掉它们会让并发批成员绕过。"""
    _write_mod("cc-pre", "export default { toolPre(p){ return {} } }")
    mgr = ModManager(enabled=True, enabled_ids=["cc-pre"])
    mod = mgr.mods["cc-pre"]
    assert mod.declared_hooks == set()  # CC 风格：清单不声明 hooks
    assert await mgr.has_tool_pre() is True
    assert mod.detected_hooks is not None  # 判定过程完成懒探测
    assert "tool_pre" in mod.dispatch_hooks()
    mgr.close()

    # 对照：CC 风格但没实现 toolPre —— 不该为它断批
    _write_mod("cc-post", "export default { toolPost(p){ return {} } }")
    mgr2 = ModManager(enabled=True, enabled_ids=["cc-post"])
    assert not await mgr2.has_tool_pre()
    mgr2.close()


async def test_undeclared_tool_pre_deny_survives_readonly_batch(home):
    """CC 风格（清单未声明 hooks）的 toolPre deny 同样断批：同轮连发两个
    READONLY 调用，第二个不得绕过 JS deny 直达执行。"""
    for name in ("a.txt", "b.txt"):
        (home / "proj" / name).write_text("x", encoding="utf-8")
    _write_mod("cc-deny",
               "export default { toolPre(p){"
               " if (p.input.path === 'b.txt') return {deny: 'b.txt is forbidden'};"
               " return {} } }",
               {"permissions": "tighten"})  # 无 hooks 声明（CC 风格）+ tighten 档 deny 有效
    mgr = ModManager(enabled=True, enabled_ids=["cc-deny"])
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"}),
         ToolUseBlock(id="t2", name="read_file", input={"path": "b.txt"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "read files")
    finished = {e.tool_call_id: e for e in events if isinstance(e, ToolCallFinished)}
    assert not finished["t1"].is_error
    assert finished["t2"].is_error
    assert "forbidden" in finished["t2"].preview
    # 断批证据：两调用交错收尾（并发批是两条 Started 连发再两条 Finished）
    order = [(e.tool_call_id, type(e).__name__)
             for e in events
             if isinstance(e, (ToolCallStarted, ToolCallFinished))]
    assert order == [("t1", "ToolCallStarted"), ("t1", "ToolCallFinished"),
                     ("t2", "ToolCallStarted"), ("t2", "ToolCallFinished")]
    mgr.close()


async def test_modui_budget_50_per_turn_with_single_overflow_notice(home):
    """ModUI 每轮上限 50 条：超出丢弃，且只发一次溢出 Notice（不打爆事件流）。

    片段预算按「解析后的 widget」计：每次 handler 结果先被词表截到 8 条
    （MAX_WIDGETS_PER_RESULT），7 次调用 × 8 = 56 > 50 触发溢出。
    """
    for name in ("a", "b", "c", "d", "e", "f", "g"):
        (home / "proj" / f"{name}.txt").write_text("x", encoding="utf-8")
    _write_mod("budget-mod",
               "export default { toolPre(p){ var ui = [];"
               " for (var i = 0; i < 10; i++) ui.push({kind: 'badge',"
               " slot: 'stream', text: 'w' + i}); return {ui: ui} } }",
               {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["budget-mod"])
    provider = FakeProvider([
        [ToolUseBlock(id=f"t{i}", name="read_file",
                      input={"path": f"{name}.txt"})
         for i, name in enumerate(("a", "b", "c", "d", "e", "f", "g"))],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "read all")
    mod_ui = [e for e in events if isinstance(e, ModUI)]
    assert len(mod_ui) == 50, "7 次调用 × 8 片段 = 56，只应下发前 50 条"
    assert all(e.mod_id == "budget-mod" and e.slot == "stream" for e in mod_ui)
    overflows = [e for e in events if isinstance(e, NoticeEvent)
                 and "已达本轮上限" in e.message]
    assert len(overflows) == 1, "溢出提醒只发一次"
    mgr.close()


async def test_widgets_per_result_capped_at_eight(home):
    """单次 handler 返回的 widget 截到 8 条（MAX_WIDGETS_PER_RESULT）。"""
    from skysheep.core.mods import MAX_WIDGETS_PER_RESULT

    _write_mod("flood-mod",
               "export default { toolPre(p){ var ui = [];"
               " for (var i = 0; i < 20; i++) ui.push({kind: 'badge',"
               " slot: 'stream', text: 'w' + i}); return {ui: ui} } }",
               {"hooks": ["tool_pre"]})
    mgr = ModManager(enabled=True, enabled_ids=["flood-mod"])
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert len(r["ui"]) == MAX_WIDGETS_PER_RESULT == 8
    assert [w["text"] for w in r["ui"]] == [f"w{i}" for i in range(8)]
    mgr.close()


# ---- WS 方法缺口：官方一键装与 mods_updated 广播 ----


def test_mods_install_official_uses_repo_gallery_fallback(home):
    """mods.install_official：bundled 缺席时回落仓库 mods-gallery，装完即启用。"""
    from skysheep.config import _read_raw_config
    from skysheep.core.mods import bundled_gallery_dir

    if bundled_gallery_dir() is None:
        pytest.skip("官方示例包在本机不可用（无打包副本且无仓库回落）")

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "o1", "method": "mods.install_official",
                      "params": {"id": "token-weather"}})
        r = recv_until(ws, "o1")
        assert r["ok"], r
        assert r["result"]["installed"] == "token-weather"
        target = mods_root() / "token-weather"
        assert (target / "mod.json").is_file()
        assert (target / "main.js").is_file()
        source = json.loads((target / ".source.json").read_text(encoding="utf-8"))
        assert source["source"] == "bundled"
        # 安装即启用：config 里进了名单，backend 热重载后已挂上
        _p, raw = _read_raw_config()
        assert "token-weather" in raw["mods"]["enabled_mods"]
        backend = client.app.state.backend
        assert backend.mods is not None and "token-weather" in backend.mods.mods
        # 官方清单把它标成已安装
        ws.send_json({"id": "o2", "method": "mods.official"})
        r = recv_until(ws, "o2")
        entry = {m["id"]: m for m in r["result"]["mods"]}["token-weather"]
        assert entry["installed"] is True

        # 非官方 id 拒绝
        ws.send_json({"id": "o3", "method": "mods.install_official",
                      "params": {"id": "not-official"}})
        assert recv_until(ws, "o3")["ok"] is False


def test_mods_updated_broadcast_reaches_ws_clients(home):
    """装删启停后广播 mods_updated：在线前端能收到（照 mcp_updated 模式）。"""
    _write_mod("b-mod", "export default {}", {"hooks": ["tool_pre"]})
    events: list = []
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "mods.toggle",
                      "params": {"id": "b-mod", "enabled": True}})
        assert recv_until(ws, "t1", events)["ok"]
        # 广播帧与回执的先后不保证：再发一帧探测请求，把排队中的事件收进来
        ws.send_json({"id": "probe", "method": "mods.list"})
        recv_until(ws, "probe", events)
        kinds = [e.get("event") for e in events]
        assert "mods_updated" in kinds, f"toggle 后应收到广播；收到的事件：{kinds}"


# ---- 既有回归的护栏 ----


def test_modui_is_not_a_mergeable_delta_kind():
    """ModUI 是全量状态帧：不进 StreamDeltaMerger 的可合并集合（冲刷语义不被破坏）。"""
    from skysheep.server.backend import _MERGEABLE_DELTA_KINDS

    assert "mod_ui" not in _MERGEABLE_DELTA_KINDS
