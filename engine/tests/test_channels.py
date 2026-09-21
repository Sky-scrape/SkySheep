"""聊天软件渠道（Bot Channel）测试。

不打真实网络：Telegram 适配器用 httpx.MockTransport 承载，验证请求形状与
消息归一化；权限门与命令解析是纯逻辑，直接单测。

安全相关的断言是本文件的重点——空名单必须拒绝一切、无人值守时必须自动拒绝
写操作、审批超时必须自动拒绝。这三条错了就是把 Agent 的控制权交出去。
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from skysheep.channels.commands import parse as parse_command
from skysheep.channels.gate import ChannelGate, parse_decision
from skysheep.channels.manager import ChannelManager
from skysheep.channels.telegram import TelegramChannel, _split
from skysheep.security.gate import Decision
from skysheep.tools.base import Safety, Tool


class _WriteTool(Tool):
    name = "write_file"
    description = "写入文件"
    safety = Safety.WRITE

    async def run(self, args, ctx):  # pragma: no cover - 测试不执行
        return ""


class _ReadTool(Tool):
    name = "read_file"
    description = "读取文件"
    safety = Safety.READONLY

    async def run(self, args, ctx):  # pragma: no cover - 测试不执行
        return ""


# ---------- 命令解析 ----------


def test_command_parsing():
    assert parse_command("/status").name == "status"
    assert parse_command("/help").name == "help"
    assert parse_command("/状态").name == "status"
    # Telegram 群聊里命令带 @botname 后缀
    assert parse_command("/stop@my_bot").name == "stop"
    # 未知命令单独标记：不能当成普通消息送给模型，否则打错命令会得到莫名回答
    assert parse_command("/nope").name == "__unknown__"
    # 普通文本原样交给 Agent
    assert parse_command("帮我看下项目结构").is_command is False
    assert parse_command("").is_command is False
    # 不以 / 开头的斜杠用法（如路径）不应被当命令
    assert parse_command("a/b").is_command is False


def test_approval_word_parsing():
    assert parse_decision("allow") == Decision.ALLOW_ONCE
    assert parse_decision("ALLOW") == Decision.ALLOW_ONCE
    assert parse_decision("allow always") == Decision.ALLOW_ALWAYS
    assert parse_decision("总是允许") == Decision.ALLOW_ALWAYS
    assert parse_decision("deny") == Decision.DENY
    assert parse_decision("拒绝") == Decision.DENY
    # 关键：普通聊天内容不能被误判成审批回复
    assert parse_decision("allow 我看看这个文件") is None
    assert parse_decision("随便说点什么") is None
    assert parse_decision("") is None
    # 模糊匹配是危险的：这些都不该命中
    for word in ("yes please", "no idea", "okay then"):
        assert parse_decision(word) is None, word


# ---------- 权限门：安全核心 ----------


async def test_readonly_always_allowed():
    gate = ChannelGate(approve_enabled=False)
    assert await gate.authorize(_ReadTool(), {}) is None


async def test_headless_mode_denies_writes_without_blocking():
    """未开启审批时写操作必须立刻被拒绝，而不是挂起等前端。"""
    gate = ChannelGate(approve_enabled=False)
    pending = await gate.authorize(_WriteTool(), {"path": "x"})
    assert pending is not None
    assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY


async def test_pretool_allowed_list_passes():
    gate = ChannelGate(allowed=["write_file"], approve_enabled=False)
    assert await gate.authorize(_WriteTool(), {}) is None


async def test_approval_timeout_denies_and_cleans_up():
    """超时必须回 deny（而不是抛 CancelledError），且清空等待表。"""
    notified = []

    async def notify(pending):
        notified.append(pending.request_id)

    gate = ChannelGate(approve_enabled=True, approve_timeout=1, notify=notify)
    pending = await gate.authorize(_WriteTool(), {"path": "x"})
    assert notified == [pending.request_id]
    assert await asyncio.wait_for(pending.wait(), timeout=5) == Decision.DENY
    assert gate.waiting == {}


async def test_approval_allow_from_chat():
    gate = ChannelGate(approve_enabled=True, approve_timeout=5, notify=lambda p: _noop())

    async def answer():
        await asyncio.sleep(0.1)
        assert gate.submit_latest(Decision.ALLOW_ONCE) is not None
        # 已投递过的请求不能再被投第二次
        assert gate.submit_latest(Decision.ALLOW_ONCE) is None

    asyncio.create_task(answer())
    pending = await gate.authorize(_WriteTool(), {"path": "x"})
    assert await asyncio.wait_for(pending.wait(), timeout=5) == Decision.ALLOW_ONCE


async def test_notify_failure_degrades_to_deny():
    """推卡片失败要退化成拒绝，绝不能挂着等一个永远不来的回复。"""

    async def bad_notify(pending):
        raise RuntimeError("网络不通")

    gate = ChannelGate(approve_enabled=True, approve_timeout=30, notify=bad_notify)
    pending = await gate.authorize(_WriteTool(), {})
    assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY
    assert gate.waiting == {}


async def test_submit_without_waiting_returns_false():
    gate = ChannelGate(approve_enabled=True)
    assert gate.submit_latest(Decision.ALLOW_ONCE) is None
    assert gate.submit("nonexistent", Decision.ALLOW_ONCE) is False


async def _noop():
    return None


# ---------- 来源允许名单 ----------


def test_empty_allowlist_denies_everything():
    """空名单 = 拒绝一切。这是渠道安全的第一道门，不能反着来。"""
    ch = TelegramChannel({"token": "t", "allowed_ids": []}, _noop_msg)
    assert ch.is_allowed("123", "123") is False
    assert ch.is_allowed("999", "999") is False


def test_allowlist_matches_actor_or_chat():
    ch = TelegramChannel({"token": "t", "allowed_ids": ["123"]}, _noop_msg)
    assert ch.is_allowed("123", "123") is True
    # 群聊里 from.id != chat.id，任一命中即可
    assert ch.is_allowed("123", "456") is True
    assert ch.is_allowed("456", "123") is True
    assert ch.is_allowed("456", "789") is False


async def _noop_msg(msg):  # pragma: no cover - 仅作为占位回调
    return None


# ---------- Telegram 适配器（MockTransport，不打真实网络） ----------


def _mock_transport(handler):
    return httpx.MockTransport(handler)


async def test_telegram_poll_normalizes_message():
    captured = []

    async def on_message(msg):
        captured.append(msg)

    delivered = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "getUpdates" not in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {}})
        # 第一次是启动清积压（timeout=0），之后只投递一次消息
        if body.get("timeout") == 0:
            return httpx.Response(200, json={"ok": True, "result": []})
        if delivered["n"] == 0:
            delivered["n"] += 1
            return httpx.Response(200, json={
                "ok": True,
                "result": [{
                    "update_id": 5,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 12345},
                        "from": {"id": 12345},
                        "text": "你好",
                    },
                }],
            })
        return httpx.Response(200, json={"ok": True, "result": []})

    ch = TelegramChannel({"token": "t", "allowed_ids": ["12345"]}, on_message)
    ch._transport = _mock_transport(handler)
    await ch.start()
    await asyncio.sleep(0.3)
    await ch.stop()

    assert len(captured) == 1, "同一 update_id 只能被处理一次"
    msg = captured[0]
    assert msg.channel == "telegram"
    assert msg.chat_id == "12345"
    assert msg.actor == "12345"
    assert msg.text == "你好"
    assert msg.approved is True


async def test_telegram_unapproved_source_marked_and_offset_advances():
    captured = []

    async def on_message(msg):
        captured.append(msg)

    delivered = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "getUpdates" not in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {}})
        body = json.loads(request.content)
        if body.get("timeout") == 0:
            return httpx.Response(200, json={"ok": True, "result": []})
        if delivered["n"] == 0:
            delivered["n"] += 1
            return httpx.Response(200, json={
                "ok": True,
                "result": [{
                    "update_id": 9,
                    "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "hi"},
                }],
            })
        return httpx.Response(200, json={"ok": True, "result": []})

    ch = TelegramChannel({"token": "t", "allowed_ids": ["12345"]}, on_message)
    ch._transport = _mock_transport(handler)
    await ch.start()
    await asyncio.sleep(0.3)
    await ch.stop()

    assert len(captured) == 1
    assert captured[0].approved is False
    # offset 必须推进，否则同一条消息会被反复处理
    assert ch._offset >= 10


async def test_telegram_text_is_split_for_long_replies():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    ch = TelegramChannel({"token": "t"}, _noop_msg)
    ch._transport = _mock_transport(handler)
    ok = await ch.send_text("1", "x" * 9000)
    assert ok is True
    assert len(sent) >= 3
    assert all(len(m["text"]) <= 3900 for m in sent)


async def test_telegram_send_reports_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    ch = TelegramChannel({"token": "bad"}, _noop_msg)
    ch._transport = _mock_transport(handler)
    assert await ch.send_text("1", "hi") is False
    assert "401" in ch.error


async def test_telegram_without_token_is_not_configured():
    ch = TelegramChannel({"token": ""}, _noop_msg)
    assert ch.configured() is False
    await ch.start()
    assert ch.running is False
    assert "Token" in ch.error


def test_split_helpers():
    assert _split("", 10) == []
    assert _split("short", 10) == ["short"]
    chunks = _split("a" * 25, 10)
    assert len(chunks) == 3
    assert "".join(chunks) == "a" * 25


# ---------- 管理器路由 ----------


class _FakeHost:
    """最小 host：只实现 ChannelManager 依赖的方法。"""

    def __init__(self):
        self.ran = []
        self.sessions = {}
        self.decisions = []
        self.chats = []
        self.store = None

    async def channel_ensure_session(self, name):
        return self.sessions.setdefault(name, f"sess-{name}")

    async def channel_new_session(self, name):
        self.sessions[name] = f"sess-{name}-new"
        return self.sessions[name]

    async def channel_run(self, session_id, text):
        self.ran.append((session_id, text))
        return {"text": "回复：" + text}

    async def channel_stop(self, session_id):
        self.stopped = session_id

    async def channel_status_text(self, session_id):
        return f"状态 {session_id}"

    async def channel_list_sessions(self):
        return [{"id": "s1", "title": "会话一"}]

    async def channel_submit_decision(self, name, decision):
        self.decisions.append((name, decision))
        return bool(getattr(self, "waiting", False))

    def note_channel_chat(self, name, chat_id):
        self.chats.append((name, chat_id))


class _RecordingChannel(TelegramChannel):
    """把发出的消息记下来，替代真实 HTTP。"""

    def __init__(self, config, on_message):
        super().__init__(config, on_message)
        self.out = []

    async def send_text(self, chat_id, text):
        self.out.append((chat_id, text))
        return True


def _manager(config):
    host = _FakeHost()
    mgr = ChannelManager(host, lambda: config)
    ch = _RecordingChannel(config.get("telegram") or {}, mgr._on_message)
    mgr.channels["telegram"] = ch
    return mgr, host, ch


async def test_manager_ignores_unapproved_source_silently():
    mgr, host, ch = _manager({"telegram": {"enabled": True, "allowed_ids": []}})
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="9", chat_id="9", text="你好", approved=False,
    ))
    # 关键：不回复。回复等于向陌生人确认机器人是活的。
    assert ch.out == []
    assert host.ran == []
    assert mgr.status()["channels"][0]["seen_sources"], "未授权来源应被记入待认领列表"


async def test_manager_runs_prompt_for_approved_source():
    mgr, host, ch = _manager({"telegram": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="9", chat_id="9", text="看下项目", approved=True,
    ))
    assert host.ran == [("sess-telegram", "看下项目")]
    assert ch.out and "回复：看下项目" in ch.out[0][1]
    assert host.chats == [("telegram", "9")]


async def test_manager_commands_do_not_hit_agent():
    mgr, host, ch = _manager({"telegram": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="9", chat_id="9", text="/status", approved=True,
    ))
    assert host.ran == []
    assert ch.out and "状态" in ch.out[0][1]

    ch.out.clear()
    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="9", chat_id="9", text="/nope", approved=True,
    ))
    assert host.ran == []
    assert ch.out and "未知命令" in ch.out[0][1]


async def test_manager_routes_approval_reply_before_agent():
    """待审批时回 allow 必须是决定，不能当成新消息再跑一轮。"""
    mgr, host, ch = _manager({"telegram": {"enabled": True, "allowed_ids": ["9"]}})
    host.waiting = True
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="9", chat_id="9", text="allow", approved=True,
    ))
    assert host.decisions == [("telegram", Decision.ALLOW_ONCE)]
    assert host.ran == [], "审批回复不应触发新一轮 Agent 运行"

    # 没有待审批项时，allow 这种词应作为普通消息正常送进 Agent
    host.waiting = False
    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="9", chat_id="9", text="allow", approved=True,
    ))
    assert host.ran == [("sess-telegram", "allow")]


async def test_manager_reports_run_error_back_to_chat():
    mgr, host, ch = _manager({"telegram": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    async def boom(session_id, text):
        raise RuntimeError("模型未配置")

    host.channel_run = boom
    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="9", chat_id="9", text="你好", approved=True,
    ))
    assert ch.out and "模型未配置" in ch.out[0][1]


async def test_manager_disabled_channel_is_not_started():
    mgr = ChannelManager(_FakeHost(), lambda: {"telegram": {"enabled": False, "token": "t"}})
    await mgr.restart()
    status = mgr.status()
    assert status["channels"][0]["running"] is False
    await mgr.stop()


async def test_manager_enabled_without_token_reports_error():
    mgr = ChannelManager(_FakeHost(), lambda: {"telegram": {"enabled": True, "token": ""}})
    await mgr.restart()
    item = mgr.status()["channels"][0]
    assert item["running"] is False
    assert "Token" in item["error"]
    await mgr.stop()


async def test_onboarding_claim_flow_after_empty_allowlist_enable():
    """首次配置闭环：名单为空也能启用 → 陌生人消息只记进「发现的来源」→
    认领后同一条来源的消息才被真正处理。

    回归背景：启用曾被要求名单非空，而 chat id 只能由运行中的机器人发现，
    两个条件互斥，首次配置永远卡死（用户视角就是「渠道用不了」）。
    """
    from skysheep.channels.base import ChannelMessage

    config = {"telegram": {"enabled": True, "token": "t", "allowed_ids": []}}
    mgr, host, ch = _manager(config)

    # 启用后机器人开始工作；第一条消息来自名单外 → 只记录，不回复、不执行
    await mgr._on_message(ChannelMessage(
        channel="telegram", actor="777", chat_id="777", text="你好", approved=False))
    assert ch.out == [] and host.ran == []
    seen = mgr.status()["channels"][0]["seen_sources"]
    assert [s["chat_id"] for s in seen] == ["777"]

    # 用户在界面上点「加入允许名单」→ 名单更新（模拟 channel.save 后的重建）
    config["telegram"]["allowed_ids"] = ["777"]
    mgr2, host2, ch2 = _manager(config)
    await mgr2._on_message(ChannelMessage(
        channel="telegram", actor="777", chat_id="777", text="看下项目", approved=True))
    assert host2.ran == [("sess-telegram", "看下项目")]
    assert ch2.out and ch2.out[0][0] == "777"


async def test_enabled_channel_with_empty_allowlist_stays_silent():
    """空名单运行中：陌生来源反复发消息也绝不回复、绝不执行（安全边界不因放开启用而松动）。"""
    from skysheep.channels.base import ChannelMessage

    mgr, host, ch = _manager({"telegram": {"enabled": True, "token": "t", "allowed_ids": []}})
    for i in range(3):
        await mgr._on_message(ChannelMessage(
            channel="telegram", actor="e", chat_id="e", text=f"第{i}条", approved=False))
    assert ch.out == [] and host.ran == []
    seen = mgr.status()["channels"][0]["seen_sources"]
    assert len(seen) == 1 and seen[0]["count"] == 3


# ---------- 配置读写 ----------


def test_channels_config_defaults_and_roundtrip(home):
    from skysheep.config import load_config, update_config_section

    cfg = load_config()
    assert cfg.channels.platforms == {}
    assert cfg.channels.approve_timeout == 120

    update_config_section("channels", {"platforms": {"telegram": {
        "enabled": True, "token": "abc", "allowed_ids": ["1", "2"],
    }}})
    cfg2 = load_config()
    tg = cfg2.channels.platforms["telegram"]
    assert tg["enabled"] is True
    assert tg["token"] == "abc"
    assert tg["allowed_ids"] == ["1", "2"]


def test_channels_config_normalizes_numeric_ids(home):
    """TOML 里把 chat id 写成数字是常见笔误；不转成字符串会让名单静默失效。"""
    from skysheep.config import config_path, load_config

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[channels]\napprove_timeout = 60\n"
        "[channels.platforms.telegram]\nenabled = true\ntoken = \"t\"\n"
        "allowed_ids = [12345, 67890]\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.channels.approve_timeout == 60
    assert cfg.channels.platforms["telegram"]["allowed_ids"] == ["12345", "67890"]


def test_channels_config_tolerates_garbage(home):
    from skysheep.config import config_path, load_config

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[channels]\napprove_timeout = \"not-a-number\"\n"
        "[channels.platforms]\ntelegram = \"这不是配置\"\n",
        encoding="utf-8",
    )
    cfg = load_config()  # 不能抛
    assert cfg.channels.approve_timeout == 120
    assert cfg.channels.platforms == {}


# ---------- 后端与 WS 分发 ----------


@pytest.fixture
def client(home):
    from fastapi.testclient import TestClient

    from skysheep.models.fake import FakeProvider
    from skysheep.server import create_app

    provider = FakeProvider([{"text": "好的"}])
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    with TestClient(app) as c:
        yield c


def _ws_call(ws, mid, method, params=None):
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    while True:
        frame = ws.receive_json()
        if frame.get("id") == mid:
            return frame


def test_channel_status_over_ws(client):
    with client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "c1", "channel.status")
        assert frame["ok"] is True
        data = frame["result"]
        assert "telegram" in data["supported"]
        assert data["approve_timeout"] == 120


def test_channel_enable_requires_token_and_allowlist(client, home):
    with client.websocket_connect("/ws") as ws:
        # 没填 token
        frame = _ws_call(ws, "c1", "channel.enable", {"name": "telegram"})
        assert frame["ok"] is False
        assert "Token" in frame["error"]


def test_channel_enable_with_empty_allowlist_starts_polling(client, home):
    """名单为空也允许启用：chat id 只能由运行中的机器人记进「发现的来源」，
    若启用时要求名单非空，首次配置就死锁（机器人不跑 → 永远收不到第一条消息）。
    安全语义不变：空名单 = 拒绝一切，由消息层强制，机器人对陌生人保持沉默。"""
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "c1", "channel.save", {"name": "telegram", "token": "abc"})
        frame = _ws_call(ws, "c2", "channel.enable", {"name": "telegram"})
        assert frame["ok"] is True
        tg = next(c for c in frame["result"]["channels"] if c["name"] == "telegram")
        assert tg["enabled"] is True
        assert tg["running"] is True, "启用后必须真的开始轮询，否则永远发现不了来源"
        assert tg["allowed_ids"] == []


def test_channel_enable_persists_and_disables(client, home):
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "c1", "channel.save", {
            "name": "telegram", "token": "abc", "allowed_ids": "12345\n67890",
        })
        frame = _ws_call(ws, "c2", "channel.enable", {"name": "telegram"})
        assert frame["ok"] is True
        tg = next(c for c in frame["result"]["channels"] if c["name"] == "telegram")
        assert tg["enabled"] is True
        assert tg["allowed_ids"] == ["12345", "67890"]
        # token 不回显，只回「已填」
        assert tg["has_token"] is True
        assert "token" not in tg

        frame = _ws_call(ws, "c3", "channel.disable", {"name": "telegram"})
        tg = next(c for c in frame["result"]["channels"] if c["name"] == "telegram")
        assert tg["enabled"] is False


def test_channel_save_does_not_wipe_token_when_omitted(client, home):
    """界面保存允许名单时不该把已存的 Token 清掉。"""
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "c1", "channel.save", {"name": "telegram", "token": "secret"})
        frame = _ws_call(ws, "c2", "channel.save", {
            "name": "telegram", "allowed_ids": "999",
        })
        tg = next(c for c in frame["result"]["channels"] if c["name"] == "telegram")
        assert tg["has_token"] is True
        assert tg["allowed_ids"] == ["999"]


def test_channel_set_timeout_clamps(client):
    with client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "c1", "channel.set_timeout", {"approve_timeout": 5})
        assert frame["result"]["approve_timeout"] == 10  # 下限
        frame = _ws_call(ws, "c2", "channel.set_timeout", {"approve_timeout": 99999})
        assert frame["result"]["approve_timeout"] == 3600  # 上限


def test_channel_session_binding_survives_runtime_loss(home):
    """绑定存在但 runtime 不在时（重启后首次使用）必须能自愈，而不是报「会话不存在」。"""
    import asyncio

    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider
    from skysheep.server.backend import ServerBackend

    async def scenario():
        be = ServerBackend(
            working_dir=home / "proj",
            provider_name="fake",
            provider_factory=lambda: FakeProvider([]).with_default([TextBlock(text="好")]),
        )
        await be.setup()
        try:
            sid = await be.channel_ensure_session("telegram")
            assert sid in be.runtimes
            # 模拟 runtime 丢失（重启后尚未重建）
            be.runtimes.clear()
            be._channel_gates.clear()
            be._channel_names.clear()

            # ensure 要能补建
            again = await be.channel_ensure_session("telegram")
            assert again == sid
            assert sid in be.runtimes

            # channel_run 也要能自愈
            be.runtimes.clear()
            be._channel_names.clear()
            result = await be.channel_run(sid, "你好")
            assert "error" not in result, result
            assert result["text"] == "好"

            # 非渠道会话应给出可操作的提示，而不是模糊报错
            be.runtimes.clear()
            be._channel_names.clear()
            result = await be.channel_run("not-a-channel-session", "你好")
            assert "error" in result
            assert "/new" in result["error"]
        finally:
            await be.shutdown()

    asyncio.run(scenario())


def test_channel_remote_cannot_change_settings(home, monkeypatch):
    """远端（局域网 / tailnet）只能看状态，不能改渠道——避免被挟持的手机把 Agent 接出去。"""
    from fastapi.testclient import TestClient

    from skysheep.config import update_config_section
    from skysheep.models.fake import FakeProvider
    from skysheep.server import create_app

    update_config_section("server", {"lan": True, "token": "testtoken123456"})
    provider = FakeProvider([{"text": "ok"}])
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    import skysheep.server.app as app_mod

    with TestClient(app) as c:
        # 伪造远端来源：TestClient 默认是回环（local），把来源分类掰成 other
        monkeypatch.setattr(app_mod, "client_origin", lambda _c: "other")
        with c.websocket_connect("/ws?token=testtoken123456") as ws:
            # 读状态允许
            frame = _ws_call(ws, "c1", "channel.status")
            assert frame["ok"] is True
            # 改配置必须被拒
            frame = _ws_call(ws, "c2", "channel.enable", {"name": "telegram"})
            assert frame["ok"] is False
            assert "桌面端" in frame["error"]


# ---------- 微信 iLink 渠道 ----------
# 协议形状取自腾讯官方 npm 包 @tencent-weixin/openclaw-weixin 的源码，
# 用 httpx.MockTransport 复刻响应，不打真实网络。


def _wx(config=None, on_message=None):
    from skysheep.channels.weixin import WeixinChannel

    return WeixinChannel(config or {}, on_message or _noop_msg)


def test_weixin_headers_match_official_shape():
    ch = _wx({"bot_token": "tok"})
    h = ch._headers("tok")
    # 官方包的固定头：iLink-App-Id 来自 package.json 的 ilink_appid（值 "bot"）
    assert h["iLink-App-Id"] == "bot"
    assert h["AuthorizationType"] == "ilink_bot_token"
    assert h["Authorization"] == "Bearer tok"
    # X-WECHAT-UIN 是防重放：随机 uint32 → 十进制字符串 → base64
    raw = base64.b64decode(h["X-WECHAT-UIN"]).decode("utf-8")
    assert raw.isdigit()
    assert 0 <= int(raw) <= 0xFFFFFFFF


def test_weixin_extract_text_from_item_list():
    from skysheep.channels.weixin import _extract_text

    assert _extract_text({"item_list": [{"type": 1, "text_item": {"text": "你好"}}]}) == "你好"
    # 非文本 item 跳过，取第一条真正的文本
    assert _extract_text({"item_list": [{"type": 2}, {"type": 1, "text_item": {"text": "x"}}]}) == "x"
    assert _extract_text({"item_list": []}) == ""
    assert _extract_text({}) == ""


def test_weixin_not_configured_without_login():
    ch = _wx({})
    assert ch.configured() is False


async def test_weixin_start_without_token_marks_relogin():
    ch = _wx({})
    await ch.start()
    assert ch.running is False
    assert ch.need_relogin is True
    assert "扫码" in ch.error


async def test_weixin_fetch_qrcode_and_poll():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "get_bot_qrcode" in url:
            return httpx.Response(200, json={
                "qrcode": "qr123", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/x", "ret": 0,
            })
        if "get_qrcode_status" in url:
            return httpx.Response(200, json={"status": "wait", "ret": 0})
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({})
    ch._transport = _mock_transport(handler)
    info = await ch.fetch_qrcode()
    assert info["qrcode"] == "qr123"
    assert info["url"].startswith("https://")
    # 契约关键：url 是「要编码进二维码的链接」，不是图片地址。
    # 前端必须用二维码库渲染它（直接塞 <img src> 会因返回的是网页而破图）。
    assert "liteapp.weixin.qq.com" in info["url"]
    st = await ch.poll_qrcode("qr123")
    assert st["status"] == "wait"
    await ch.stop()


async def test_weixin_login_confirmed_returns_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        if "get_qrcode_status" in str(request.url):
            return httpx.Response(200, json={
                "status": "confirmed", "ret": 0,
                "credentials": {"bot_token": "bt-1", "ilink_bot_id": "b1", "ilink_user_id": "u1"},
                "baseurl": "https://ilinkai.weixin.qq.com",
            })
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({})
    ch._transport = _mock_transport(handler)
    st = await ch.poll_qrcode("qr")
    assert st["status"] == "confirmed"
    assert st["bot_token"] == "bt-1"
    await ch.stop()


async def test_weixin_apply_login_persists_state():
    saved = []

    async def on_state(state):
        saved.append(state)

    ch = _wx({})
    ch.on_state = on_state
    await ch.apply_login("tok-xyz", "https://ilinkai.weixin.qq.com")
    assert ch.configured() is True
    assert ch.need_relogin is False
    assert saved and saved[0]["bot_token"] == "tok-xyz"


async def test_weixin_poll_advances_cursor_and_checks_allowlist():
    """游标必须推进并落盘，否则重启后会重复收到旧消息。"""
    captured = []
    saved = []

    async def on_message(msg):
        captured.append(msg)

    async def on_state(state):
        saved.append(dict(state))

    def handler(request: httpx.Request) -> httpx.Response:
        if "getupdates" in str(request.url):
            return httpx.Response(200, json={
                "ret": 0, "get_updates_buf": "cursor-1",
                "msgs": [{
                    "from_user_id": "userA", "message_type": 1,
                    "context_token": "ctx-1",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }],
            })
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({"bot_token": "t", "allowed_ids": ["userA"]}, on_message)
    ch.on_state = on_state
    ch._transport = _mock_transport(handler)
    await ch._poll_once()
    assert ch.cursor == "cursor-1"
    assert any(s.get("cursor") == "cursor-1" for s in saved), "游标要落盘"
    assert captured and captured[0].text == "你好"
    assert captured[0].approved is True

    # 名单外来源：消息仍会构造出来，但 approved=False，由路由层拒绝
    ch2 = _wx({"bot_token": "t", "allowed_ids": ["other"]}, on_message)
    ch2._transport = _mock_transport(handler)
    captured.clear()
    await ch2._poll_once()
    assert captured and captured[0].approved is False


async def test_weixin_send_includes_context_token():
    """回复必须带 context_token，否则消息落不到正确窗口。"""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ret": 0, "msg_id": "m1"})

    ch = _wx({"bot_token": "t"})
    ch._transport = _mock_transport(handler)
    await ch._handle_message({
        "from_user_id": "userA", "message_type": 1, "context_token": "ctx-abc",
        "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
    })
    assert await ch.send_text("userA", "回复内容") is True
    assert bodies
    msg = bodies[-1]["msg"]
    assert msg["context_token"] == "ctx-abc"
    assert msg["to_user_id"] == "userA"
    assert msg["message_type"] == 2    # BOT
    assert msg["message_state"] == 2   # FINISH
    assert msg["item_list"][0]["text_item"]["text"] == "回复内容"
    assert "client_id" in msg
    await ch.stop()


async def test_weixin_send_without_context_still_sends():
    """没记过 context_token 时仍应发出（官方实现也只是警告不阻断）。"""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({"bot_token": "t"})
    ch._transport = _mock_transport(handler)
    assert await ch.send_text("userA", "hi") is True
    assert "context_token" not in bodies[-1]["msg"]
    await ch.stop()


async def test_weixin_stale_token_triggers_cooldown():
    """-14（session timeout）要进入冷却并标记需重新登录，而不是疯狂重试。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": -14, "errmsg": "session timeout"})

    ch = _wx({"bot_token": "stale"})
    ch._transport = _mock_transport(handler)
    await ch._poll_once()
    assert ch.need_relogin is True
    assert ch.paused is True
    assert await ch.send_text("userA", "hi") is False  # 冷却期间不再发
    await ch.stop()


async def test_weixin_ignores_bot_own_messages():
    """message_type=2 是机器人自己发的，不能当成用户消息回灌。"""
    captured = []

    async def on_message(msg):
        captured.append(msg)

    ch = _wx({"bot_token": "t", "allowed_ids": ["u"]}, on_message)
    await ch._handle_message({
        "from_user_id": "u", "message_type": 2,
        "item_list": [{"type": 1, "text_item": {"text": "我自己发的"}}],
    })
    assert captured == []


async def test_weixin_poll_backs_off_when_no_cursor_advance():
    """游标不变时必须让出控制权，否则紧密空转饿死事件循环。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": 0, "get_updates_buf": "", "msgs": []})

    ch = _wx({"bot_token": "t"})
    ch._transport = _mock_transport(handler)
    task = asyncio.ensure_future(ch._poll_once())
    done, pending = await asyncio.wait({task}, timeout=0.2)
    assert not done, "无进展时应停留在退避里"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await ch.stop()


def test_weixin_parse_login_state_tolerates_garbage():
    from skysheep.channels.weixin import parse_login_state

    assert parse_login_state("") == {}
    assert parse_login_state("not json") == {}
    assert parse_login_state("[1,2]") == {}
    assert parse_login_state('{"bot_token":"t"}') == {"bot_token": "t"}


# ---------- 微信：WS 分发与配置 ----------


def test_channel_status_lists_weixin(client):
    with client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "w1", "channel.status")
        assert frame["ok"] is True
        names = [c["name"] for c in frame["result"]["channels"]]
        assert "weixin" in names, "微信渠道要出现在界面数据里"
        wx = next(c for c in frame["result"]["channels"] if c["name"] == "weixin")
        assert wx["has_login"] is False
        assert wx["needs_qr"] is True


def test_weixin_enable_requires_login(client, home):
    """微信的凭据来自扫码，不是手填 Token——未登录时启用要给明确指引。"""
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "w1", "channel.save", {"name": "weixin", "allowed_ids": "u1"})
        frame = _ws_call(ws, "w2", "channel.enable", {"name": "weixin"})
        assert frame["ok"] is False
        assert "扫码" in frame["error"]


def test_weixin_logout_clears_credentials(client, home):
    from skysheep.config import load_config, update_config_section

    update_config_section("channels", {"platforms": {
        "weixin": {"enabled": False, "bot_token": "tok", "allowed_ids": ["u1"]},
    }})
    with client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "w1", "channel.weixin_logout")
        assert frame["ok"] is True
    cfg = load_config()
    assert "bot_token" not in cfg.channels.platforms["weixin"]


def test_weixin_login_methods_blocked_for_remote(home, monkeypatch):
    """扫码登录会拿到能操控 Agent 的凭据，必须只允许本机。"""
    from fastapi.testclient import TestClient

    from skysheep.config import update_config_section
    from skysheep.models.fake import FakeProvider
    from skysheep.server import create_app

    update_config_section("server", {"lan": True, "token": "testtoken123456"})
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: FakeProvider([{"text": "ok"}]),
    )
    import skysheep.server.app as app_mod

    with TestClient(app) as c:
        monkeypatch.setattr(app_mod, "client_origin", lambda _c: "other")
        with c.websocket_connect("/ws?token=testtoken123456") as ws:
            frame = _ws_call(ws, "w1", "channel.weixin_login_start")
            assert frame["ok"] is False
            assert "本机" in frame["error"]


def test_channels_config_keeps_weixin_runtime_state(home):
    """bot_token 与游标要能落盘（它们是运行时凭据，不是用户手填的）。"""
    from skysheep.config import load_config, update_config_section

    update_config_section("channels", {"platforms": {
        "weixin": {"bot_token": "bt", "cursor": "c1", "allowed_ids": ["u"]},
    }})
    cfg = load_config()
    wx = cfg.channels.platforms["weixin"]
    assert wx["bot_token"] == "bt"
    assert wx["cursor"] == "c1"
    assert wx["allowed_ids"] == ["u"]



# ---------- 多渠道隔离（回归） ----------


async def test_channels_do_not_share_sessions(home):
    """每个渠道要有自己的会话与会话标题，不能串台。

    这个 bug 真实发生过：路由用适配器的 name 属性，而字典键用注册键，
    两者不一致时微信的回复会落到 Telegram 的会话里（/status 显示错平台）。
    现在由 ChannelManager 在注册时把字典键写回适配器实例，此处锁住该行为。
    """
    from skysheep.channels.base import ChannelMessage
    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider
    from skysheep.server.backend import ServerBackend

    be = ServerBackend(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: FakeProvider([]).with_default([TextBlock(text="好的。")]),
    )
    await be.setup()
    try:
        mgr = be.channels
        mgr.channels.clear()
        tg = _RecordingChannel({"allowed_ids": ["t1"]}, mgr._on_message)
        wx = _RecordingChannel({"bot_token": "k", "allowed_ids": ["w1"]}, mgr._on_message)
        tg.name = "telegram"
        wx.name = "weixin"
        mgr.channels["telegram"] = tg
        mgr.channels["weixin"] = wx

        await mgr._on_message(ChannelMessage(
            channel="weixin", actor="w1", chat_id="w1", text="你好", approved=True))
        await mgr._on_message(ChannelMessage(
            channel="telegram", actor="t1", chat_id="t1", text="你好", approved=True))

        sid_wx = await be.store.get_channel_binding("weixin")
        sid_tg = await be.store.get_channel_binding("telegram")
        assert sid_wx and sid_tg
        assert sid_wx != sid_tg, "两个渠道必须是独立会话"

        sess_wx = await be.store.get_session(sid_wx)
        sess_tg = await be.store.get_session(sid_tg)
        assert "weixin" in sess_wx.title
        assert "telegram" in sess_tg.title
    finally:
        await be.shutdown()


async def test_manager_sets_adapter_name_from_registry_key():
    """注册键要写回适配器实例，让事件里的 channel 字段与字典键一致。"""
    from skysheep.channels.manager import ChannelManager

    mgr = ChannelManager(_FakeHost(), lambda: {"telegram": {"enabled": False, "token": "t"}})
    await mgr.restart()
    ch = mgr.channels["telegram"]
    assert ch.name == "telegram"
    await mgr.stop()
