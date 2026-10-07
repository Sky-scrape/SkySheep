"""真实 socket 上的 Mods 扩展（实验性）端到端测试。

test_mods.py / test_server.py 的 Mods 用例不经过真实网络栈（进程内或
TestClient 直调）；这里补真实 uvicorn + websockets socket 一层，只覆盖
真实握手/分帧链路（test_ws_e2e.py 同款纪律）：

- 装 token-weather → 发消息 → 断言 mod_ui(tray) 事件经真实 socket 到达；
- 装 blast-radius → 触发 run_command（白名单命中也拦）→ 断言确认卡 payload
  带 mod_note 且必须人工决策（无人应答时轮次挂着）。

用 FakeProvider 脚本回放，不需要网络与任何外部凭据；文件级 e2e 标记：
``pytest -m e2e`` 单独跑、``pytest -m "not e2e"`` 跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
import websockets
from test_ws_e2e import _rpc

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home, provider):
    import uvicorn

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


def _install_mod(home, mod_id, main_js, manifest):
    from skysheep.config import set_mods_in_config
    from skysheep.core.mods import mods_root

    mod_dir = mods_root() / mod_id
    mod_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"id": mod_id, "name": mod_id, "version": "1.0.0",
                "permissions": "observe", **manifest}
    manifest["id"] = mod_id
    (mod_dir / "mod.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")
    # e2e 场景总开关开 + 启用该 Mod（默认关是产品行为，测试里显式打开）
    set_mods_in_config(enabled=True, enabled_mods=[mod_id])


async def _drain_until(ws, events, predicate, deadline=30.0):
    """读事件帧直到 predicate 命中；到不了就超时报错（供调试看已收事件）。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        hits = [e for e in events if predicate(e)]
        if hits:
            return hits[0]
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline))
        if "event" in frame:
            events.append(frame)
    raise AssertionError(
        "超时未等到目标事件；已收事件 kinds="
        + str([e.get("event") for e in events][-40:])
    )


async def test_real_socket_mod_ui_tray_reaches_frontend(home):
    """token-weather 的 tray 片段经真实 socket 到达前端（mod_ui 事件）。"""
    _install_mod(home, "token-weather",
                 "export default { turnStop(p){ return {ui: [{kind: 'stat',"
                 " slot: 'tray', text: '☁️ 晴 12%', level: 'ok'}]} } }",
                 {"hooks": ["turn_stop"]})
    provider = FakeProvider([[TextBlock(text="好")]])
    events: list = []
    async with live_server(home, provider) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot
            chat = await _rpc(ws, "c1", "chat.send", {"text": "hi", "session_id": ""},
                              events=events)
            assert chat.get("ok") is True, chat
            widget = await _drain_until(
                ws, events,
                lambda e: e.get("event") == "mod_ui"
                and e["data"].get("slot") == "tray",
            )
            assert widget["data"]["mod_id"] == "token-weather"
            assert widget["data"]["widget"]["kind"] == "stat"
            assert "晴" in widget["data"]["widget"]["text"]


async def test_real_socket_require_confirm_blocks_whitelisted_command(home):
    """blast-radius：declarative.require_confirm 压过白名单——确认卡照发，
    payload 带 mod_note；不发决策则轮次保持挂起（必须人工决策）。"""
    _install_mod(home, "blast-radius",
                 "export default { permissionRequest(p){"
                 " return {note: '影响面：高危命令'} } }",
                 {"hooks": ["permission_request"], "permissions": "tighten",
                  "declarative": {"deny_tools": [],
                                  "require_confirm_tools": ["run_command"]}})
    # 白名单里放行 run_command 整工具：若 Mod 收紧失效，这条命令将零确认直达执行
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="run_command",
                      input={"command": "echo harmless"})],
        [TextBlock(text="done")],
    ])
    events: list = []
    async with live_server(home, provider) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot
            added = await _rpc(ws, "w1", "whitelist.add", {
                "tool": "run_command", "kind": "always",
            })
            assert added.get("ok"), added

            # chat.send 会挂在权限确认上（无人应答不回 RPC）：发完帧直接等事件
            await ws.send(json.dumps({
                "id": "c1", "method": "chat.send",
                "params": {"text": "run echo", "session_id": ""}}))
            perm = await _drain_until(
                ws, events,
                lambda e: e.get("event") == "permission_request",
            )
            data = perm["data"]
            assert data["tool_name"] == "run_command"
            # 白名单命中也拦下了：Mod 的收紧声明在门内生效
            assert "Mods 收紧声明命中" in (data.get("note") or "")
            # mod_note 带来源前缀（第三方文本不上无标识的确认卡）
            assert "[Mod·blast-radius]" in (data.get("mod_note") or "")
            # 不发决策：轮次保持挂起（必须人工决策，绝不自动放行）
            await asyncio.sleep(0.5)
            leftovers = [p.name for p in (home / "proj").iterdir()
                         if p.name not in (".skysheep",)]
            assert leftovers == [], f"未确认不得有任何落盘：{leftovers}"
