"""聊天软件渠道（Bot Channel）测试。

不打真实网络：飞书适配器驱动官方 lark-cli 子进程，子进程用假脚本复刻（复刻官方 CLI 的
stdout NDJSON / stderr ready 标记 / 退出码契约）；微信的 REST 部分用 httpx.MockTransport
承载。权限门与命令解析是纯逻辑，直接单测。

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
from skysheep.channels.feishu import FeishuChannel, _split
from skysheep.channels.gate import ChannelGate, parse_decision
from skysheep.channels.manager import ChannelManager
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
    # 群聊里命令可能带 @botname 后缀
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
    # 低危项：随口应和不是批准（"1" 在中文聊天里是「收到」）——待审批窗口期
    # 把它们当批准会吞掉正常聊天，甚至替一次写/执行操作背书
    for word in ("ok", "1", "可以", "好的", "嗯", "收到"):
        assert parse_decision(word) is None, word
    # 明确授权的写法仍然认
    assert parse_decision("yes") == Decision.ALLOW_ONCE
    assert parse_decision("允许") == Decision.ALLOW_ONCE
    assert parse_decision("同意") == Decision.ALLOW_ONCE


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
    gate.turn_actor = "u-owner"  # P3-17 收紧：决定必须由带 id 的发起人提交

    async def answer():
        await asyncio.sleep(0.1)
        assert gate.submit_latest(Decision.ALLOW_ONCE, actor="u-owner") is not None
        # 已投递过的请求不能再被投第二次
        assert gate.submit_latest(Decision.ALLOW_ONCE, actor="u-owner") is None

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


async def test_allow_always_downgrades_to_once_from_channel():
    """渠道端没有「总是允许」：allow always 落到门控里必须降级为单次放行。

    白名单规则是持久化的——从聊天窗口写入后所有渠道会话都不再询问，
    账号被盗或手滑的代价与「少打一次 allow」完全不成比例。
    """
    gate = ChannelGate(approve_enabled=True, approve_timeout=5, notify=lambda p: _noop())
    gate.turn_actor = "u-owner"  # P3-17 收紧：决定必须由带 id 的发起人提交

    async def answer():
        await asyncio.sleep(0.1)
        # 用户在聊天窗口回复的是「总是允许」的写法
        assert gate.submit_latest(Decision.ALLOW_ALWAYS, actor="u-owner") is not None

    asyncio.create_task(answer())
    pending = await gate.authorize(_WriteTool(), {"path": "x"})
    assert await asyncio.wait_for(pending.wait(), timeout=5) == Decision.ALLOW_ONCE


async def test_approval_decision_bound_to_turn_actor():
    """审批决定只认发起人：群聊里其他成员的 allow/deny 不生效。"""
    gate = ChannelGate(approve_enabled=True, approve_timeout=5, notify=lambda p: _noop())
    gate.turn_actor = "u-owner"

    async def hijack():
        await asyncio.sleep(0.1)
        # 同群成员（chat_id 在名单内所以是 approved 的）抢答 allow：必须被拒
        assert gate.submit_latest(Decision.ALLOW_ONCE, actor="u-other") is None
        # 发起人本人回复：生效
        assert gate.submit_latest(Decision.ALLOW_ONCE, actor="u-owner") is not None

    asyncio.create_task(hijack())
    pending = await gate.authorize(_WriteTool(), {"path": "x"})
    assert await asyncio.wait_for(pending.wait(), timeout=5) == Decision.ALLOW_ONCE


async def test_approval_actor_binding_requires_id_equality():
    """审查 S-08（2026-09-25）收紧后的绑定语义：任何一方带 id 就必须相等。

    - 绑定为空 + 回复带 id（宿主漏绑/伪造 id）：拒绝——这正是「群聊里任何人
      可替发起人批准」的缺口；
    - 绑定带 id + 回复缺 id（丢失 actor 的消息）：同样拒绝；
    - 两侧都为空（平台根本不提供消息者 id）：同样拒绝（P3-17 收紧——id 是
      校验的根本依据，缺失时宁可让发起人在桌面端确认，也不开「任何人可批」
      的口子；现平台飞书/微信恒带 id，不受影响）。
    """
    gate = ChannelGate(approve_enabled=True, approve_timeout=5, notify=lambda p: _noop())

    async def answer():
        await asyncio.sleep(0.1)
        assert gate.submit_latest(Decision.ALLOW_ONCE, actor="anyone") is None

    asyncio.create_task(answer())
    pending = await gate.authorize(_WriteTool(), {"path": "x"})
    assert await asyncio.wait_for(pending.wait(), timeout=5) == Decision.DENY

    gate2 = ChannelGate(approve_enabled=True, approve_timeout=5, notify=lambda p: _noop())
    gate2.turn_actor = "u-owner"

    async def answer2():
        await asyncio.sleep(0.1)
        assert gate2.submit_latest(Decision.ALLOW_ONCE, actor="") is None

    asyncio.create_task(answer2())
    pending2 = await gate2.authorize(_WriteTool(), {"path": "x"})
    assert await asyncio.wait_for(pending2.wait(), timeout=5) == Decision.DENY


async def _noop():
    return None


# ---------- 来源允许名单 ----------


def test_empty_allowlist_denies_everything():
    """空名单 = 拒绝一切。这是渠道安全的第一道门，不能反着来。"""
    ch = FeishuChannel({"app_id": "a", "app_secret": "b", "allowed_ids": []}, _noop_msg)
    assert ch.is_allowed("ou_1", "ou_1") is False
    assert ch.is_allowed("ou_9", "ou_9") is False


def test_allowlist_matches_actor_or_chat():
    ch = FeishuChannel({"app_id": "a", "app_secret": "b", "allowed_ids": ["ou_1"]}, _noop_msg)
    assert ch.is_allowed("ou_1", "ou_1") is True
    # 群聊里 sender open_id 与 chat_id 不同，任一命中即可
    assert ch.is_allowed("ou_1", "oc_9") is True
    assert ch.is_allowed("oc_9", "ou_1") is True
    assert ch.is_allowed("ou_9", "oc_9") is False


async def _noop_msg(msg):  # pragma: no cover - 仅作为占位回调
    return None


# ---------- 飞书适配器（驱动 lark-cli 子进程，不打真实网络） ----------
#
# 协议细节（WebSocket / protobuf 帧 / ACK）已交给官方 lark-cli 维护，本适配器只负责
# 进程编排与事件归一化。所以这里测的是「编排契约」而不是「协议正确性」：用假 CLI 复刻
# 官方 CLI 的 stdout(NDJSON) / stderr(ready 标记) / 退出码契约，不打真实网络。


def _fake_cli(tmp_path, *, ready=True, events=(), exit_after=None, send_ok=True,
              init_ok=True):
    """写一个假 lark-cli 脚本，复刻官方 CLI 的对外契约。

    只依赖 Python 自身（用当前解释器执行），不引入 Node，也不依赖真实 lark-cli。
    """
    import sys as _sys

    script = tmp_path / "fake_lark_cli.py"
    payload = {
        "ready": ready,
        "events": list(events),
        "exit_after": exit_after,
        "send_ok": send_ok,
        "init_ok": init_ok,
    }
    body = [
        "import json, os, sys, time",
        "CFG = " + repr(payload),
        "args = sys.argv[1:]",
        "def out(o):",
        "    sys.stdout.write(json.dumps(o, ensure_ascii=True) + chr(10)); sys.stdout.flush()",
        "if 'config' in args and 'init' in args:",
        "    sys.stdin.read()",
        "    if CFG['init_ok']:",
        "        d = os.environ.get('LARKSUITE_CLI_CONFIG_DIR', '.')",
        "        os.makedirs(d, exist_ok=True)",
        "        aid = args[args.index('--app-id')+1]",
        "        json.dump({'appId': aid, 'brand': 'feishu'},",
        "                  open(os.path.join(d, 'config.json'), 'w'))",
        "        out({'ok': True})",
        "    else:",
        "        out({'ok': False, 'error': {'type': 'config', 'subtype': 'invalid_client',",
        "             'code': 20002, 'message': 'The client secret is invalid.',",
        "             'hint': 'run config init to set valid app_id and app_secret'}})",
        "    sys.exit(0)",
        "if 'im' in args and '+messages-send' in args:",
        "    if CFG['send_ok']:",
        "        out({'ok': True, 'data': {'message_id': 'om_sent'}})",
        "    else:",
        "        out({'ok': False, 'error': {'message': 'bot is not in the chat'}})",
        "    sys.exit(0)",
        "if 'event' in args and 'stop' in args:",
        "    out({'ok': True}); sys.exit(0)",
        "if 'event' in args and 'consume' in args:",
        "    if CFG['ready']:",
        "        sys.stderr.write('[event] ready event_key=im.message.receive_v1' + chr(10))",
        "        sys.stderr.flush()",
        "    for ev in CFG['events']:",
        "        out(ev)",
        "    if CFG['exit_after'] is None:",
        "        time.sleep(60)",
        "    else:",
        "        time.sleep(CFG['exit_after'])",
        "    sys.exit(0)",
        "out({'ok': True})",
    ]
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    bat = tmp_path / "fake_lark_cli.bat"
    bat.write_text(
        '@echo off\r\n"' + _sys.executable + '" "' + str(script) + '" %*\r\n',
        encoding="utf-8",
    )
    return str(bat)


def _event(**kw):
    """造一条与 lark-cli 实际输出同形的扁平事件（注意：content 已预渲染成纯文本）。"""
    ev = {
        "type": "im.message.receive_v1",
        "message_id": "om_1",
        "id": "om_1",
        "chat_id": "oc_abc",
        "chat_type": "p2p",
        "message_type": "text",
        "sender_id": "ou_sender",
        "sender_type": "user",
        "content": "你好",
    }
    ev.update(kw)
    return ev


def _channel(**cfg):
    """构造飞书渠道。键缺省即代表「配置里没有这个字段」，而不是用默认值填上。"""
    from skysheep.channels.feishu import FeishuChannel

    cfg.setdefault("cli_path", "")
    return FeishuChannel(cfg, _noop_msg)


def test_feishu_parses_flat_cli_event():
    """CLI 的事件体是扁平的，且 content 已是人类可读文本——直接取用，不再解 JSON。"""
    from skysheep.channels.feishu import parse_event

    msg = parse_event(_event(content="看下项目结构", chat_id="oc_1", sender_id="ou_1"))
    assert msg is not None
    assert msg["chat_id"] == "oc_1"
    assert msg["actor"] == "ou_1"
    assert msg["text"] == "看下项目结构"
    assert msg["message_id"] == "om_1"


def test_feishu_ignores_bot_own_messages():
    """机器人/应用自己发的消息必须丢掉，否则会自己回自己。"""
    from skysheep.channels.feishu import parse_event

    assert parse_event(_event(sender_type="bot")) is None
    assert parse_event(_event(sender_type="app")) is None


def test_feishu_ignores_other_events_and_non_text():
    from skysheep.channels.feishu import parse_event

    assert parse_event(_event(type="im.chat.updated_v1")) is None
    assert parse_event(_event(message_type="image")) is None
    assert parse_event(_event(content="")) is None
    assert parse_event(_event(chat_id="")) is None
    assert parse_event({}) is None
    assert parse_event("not a dict") is None


def test_feishu_group_requires_mention():
    """群聊要求有提及：避免应用被授予「接收群聊所有消息」权限后对每句话插嘴。"""
    from skysheep.channels.feishu import parse_event

    mentions = [{"key": "@_user_1", "id": "ou_bot", "name": "羊"}]
    assert parse_event(_event(chat_type="group", mentions=[])) is None
    assert parse_event(_event(chat_type="group")) is None
    msg = parse_event(_event(chat_type="group", mentions=mentions))
    assert msg is not None and msg["chat_id"] == "oc_abc"
    # 关掉该限制后，群聊无提及也放行
    assert parse_event(_event(chat_type="group"), require_mention_in_group=False) is not None


async def test_feishu_reads_event_stream_from_subprocess(tmp_path, home):
    """端到端：起假 CLI → 等 ready → 读 NDJSON → 交给 on_message。"""
    from skysheep.channels.feishu import FeishuChannel

    got = []

    async def on_msg(m):
        got.append(m)

    cli = _fake_cli(tmp_path, events=[_event(content="看下项目结构")])
    ch = FeishuChannel(
        {"app_id": "cli_x", "app_secret": "sec", "allowed_ids": ["ou_sender"],
         "cli_path": cli},
        on_msg,
    )
    try:
        await ch.start()
        for _ in range(80):
            if got:
                break
            await asyncio.sleep(0.1)
        assert ch.connected is True
        assert ch.error == ""
        assert len(got) == 1
        m = got[0]
        assert m.channel == "feishu"
        assert m.actor == "ou_sender"
        assert m.chat_id == "oc_abc"
        assert m.text == "看下项目结构"
        assert m.approved is True
    finally:
        await ch.stop()
    assert ch.connected is False


async def test_feishu_marks_unapproved_source(tmp_path, home):
    """名单外来源仍会构造出消息，但 approved=False —— 安全性由消息层强制。"""
    from skysheep.channels.feishu import FeishuChannel

    got = []

    async def on_msg(m):
        got.append(m)

    cli = _fake_cli(tmp_path, events=[_event(sender_id="ou_stranger", chat_id="oc_s")])
    ch = FeishuChannel(
        {"app_id": "cli_x", "app_secret": "sec", "allowed_ids": ["ou_known"],
         "cli_path": cli},
        on_msg,
    )
    try:
        await ch.start()
        for _ in range(80):
            if got:
                break
            await asyncio.sleep(0.1)
        assert len(got) == 1 and got[0].approved is False
    finally:
        await ch.stop()


async def test_feishu_dedups_repeated_message_id(home):
    """重连后 CLI 可能重推同一条消息，按 message_id 去重（CLI 明确要求用它）。"""
    from skysheep.channels.feishu import FeishuChannel

    got = []

    async def on_msg(m):
        got.append(m)

    ch = FeishuChannel(
        {"app_id": "cli_x", "app_secret": "sec", "allowed_ids": ["ou_sender"]}, on_msg
    )
    await ch._on_event(_event(message_id="om_same"))
    await ch._on_event(_event(message_id="om_same"))
    assert len(got) == 1


async def test_feishu_reports_ready_timeout(tmp_path, home):
    """CLI 没打出 ready 标记（如应用未开启机器人能力）时必须报错，不能静默挂着。"""
    from skysheep.channels import feishu as feishu_mod
    from skysheep.channels.feishu import FeishuChannel

    cli = _fake_cli(tmp_path, ready=False)
    ch = FeishuChannel({"app_id": "cli_x", "app_secret": "sec", "cli_path": cli}, _noop_msg)
    old = feishu_mod.READY_TIMEOUT
    feishu_mod.READY_TIMEOUT = 1.5
    try:
        with pytest.raises(RuntimeError, match="没有就绪"):
            await ch._consume_once()
    finally:
        feishu_mod.READY_TIMEOUT = old
        await ch.stop()


async def test_feishu_surfaces_invalid_credentials(tmp_path, home):
    """CLI 判定凭据无效时，原因要转成可读提示并停止重连。"""
    from skysheep.channels.feishu import FeishuChannel

    cli = _fake_cli(tmp_path, init_ok=False)
    ch = FeishuChannel({"app_id": "cli_x", "app_secret": "bad", "cli_path": cli}, _noop_msg)
    with pytest.raises(RuntimeError):
        await ch._consume_once()
    assert "client secret is invalid" in ch.error
    assert "config init" in ch.error  # 带上 CLI 给的处理提示
    await ch.stop()


def test_feishu_parses_ok_line_with_json_envelope(tmp_path, home):
    """真实 CLI 的输出是「OK 提示行 + JSON 信封」混在一段 stdout 里（实测）。

    config init 成功时只有一行 "OK: Configuration saved to ..."（无 JSON），失败时才是
    OK 行 + 错误信封连在一起——信封还可能是**多行 pretty-printed**（实测 code 20048
    就是这种形态）。解析器必须能在混杂输出里定位 JSON，否则成功会被误判成失败、
    失败原因提取不出来——这是真实发生过的 bug。
    """
    import subprocess
    from unittest.mock import patch

    from skysheep.channels.feishu import FeishuChannel

    ok_line = r"OK: Configuration saved to C:\x\config.json"
    envelope = (
        '{"ok":false,"error":{"type":"config","subtype":"invalid_client",'
        '"code":20002,"message":"The client secret is invalid.",'
        '"hint":"run `lark-cli config init` to set valid app_id and app_secret"}}'
    )
    pretty = (
        '{\n  "ok": false,\n  "error": {\n    "type": "config",\n'
        '    "subtype": "invalid_instance",\n    "code": 20048,\n'
        '    "message": "instance not found"\n  }\n}'
    )
    ch = FeishuChannel({"app_id": "cli_x", "app_secret": "bad"}, _noop_msg)
    ch.cli = "fake"

    def _run(std: str):
        def run(args, **kw):
            class P:
                stdout = std
                stderr = ""
                returncode = 0
            return P()
        return run

    with patch.object(subprocess, "run", _run(ok_line + "\n")):
        ok, payload = ch._run_cli(["config", "init", "--app-id", "c", "--app-secret-stdin"])
        assert ok is True and "error" not in payload
    with patch.object(subprocess, "run", _run(ok_line + "\n" + envelope + "\n")):
        ok2, payload2 = ch._run_cli(["config", "init", "--app-id", "c", "--app-secret-stdin"])
        assert ok2 is False
        assert "client secret is invalid" in ch._err_text(payload2)
    with patch.object(subprocess, "run", _run(ok_line + "\n" + pretty + "\n")):
        ok3, payload3 = ch._run_cli(["config", "init", "--app-id", "c", "--app-secret-stdin"])
        assert ok3 is False
        assert "instance not found" in ch._err_text(payload3)
    # 纯 NDJSON（事件流/发送成功的正常形态）不受影响
    with patch.object(subprocess, "run", _run('{"ok":true,"data":{"message_id":"om_1"}}\n')):
        ok4, payload4 = ch._run_cli(["im", "+messages-send", "--chat-id", "oc", "--text", "x"])
        assert ok4 is True and payload4["data"]["message_id"] == "om_1"


def test_feishu_accepts_real_cli_config_shape(tmp_path, home):
    """真实 CLI 的 config.json 是 {"apps": [{"appId": ...}]}（实测），
    不是顶层 appId。凭据快路径要能认出它，否则每次启动都重跑 config init。"""
    import json as _json

    from skysheep.channels.feishu import FeishuChannel
    from skysheep.config import skysheep_home

    cfg_dir = skysheep_home() / "feishu-cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        _json.dumps({"apps": [{"appId": "cli_real", "brand": "feishu"}]}),
        encoding="utf-8",
    )
    ch = FeishuChannel({"app_id": "cli_real", "app_secret": "sec"}, _noop_msg)
    # 同 appId → 不重写（返回 True 且不调 CLI）；换 appId → 才走重写路径
    assert ch._sync_cli_credentials() is True

    ch2 = FeishuChannel({"app_id": "cli_other", "app_secret": "sec"}, _noop_msg)
    assert ch2._sync_cli_credentials() is False  # 此处会真调 CLI，但没配 cli_path → 失败返回


async def test_feishu_writes_credentials_into_skysheep_home(tmp_path, monkeypatch):
    """凭据必须落进 SKYSHEEP_HOME 下，而不是 CLI 默认的 ~/.lark-cli。

    这是硬要求：默认目录既违反「用户数据一律在 ~/.skysheep」的约定，也会让
    dev 身份与安装版抢同一份凭据（多实例隔离失效）。
    """
    import json as _json

    from skysheep.channels.feishu import FeishuChannel

    home = tmp_path / "sshome"
    monkeypatch.setenv("SKYSHEEP_HOME", str(home))
    cli = _fake_cli(tmp_path)
    ch = FeishuChannel({"app_id": "cli_abc", "app_secret": "sec", "cli_path": cli}, _noop_msg)

    assert ch.config_dir() == str(home / "feishu-cli")
    assert await asyncio.to_thread(ch._sync_cli_credentials) is True
    cfg = _json.loads((home / "feishu-cli" / "config.json").read_text(encoding="utf-8"))
    assert cfg["appId"] == "cli_abc"


def test_feishu_without_credentials_is_not_configured():
    """凭据是两个字段，缺任一都不算配好——否则启用后只会在运行时报错。"""
    for cfg in ({}, {"app_id": "a"}, {"app_secret": "b"}):
        assert _channel(**cfg).configured() is False


async def test_feishu_without_cli_gives_actionable_error(tmp_path, home):
    """没装 lark-cli 时要给可操作提示（装什么、怎么指定路径），而不是崩栈。"""
    from skysheep.channels.feishu import FeishuChannel

    ch = FeishuChannel(
        {"app_id": "cli_x", "app_secret": "sec",
         "cli_path": str(tmp_path / "nope" / "lark-cli.exe")},
        _noop_msg,
    )
    assert ch.cli is None
    await ch.start()
    assert ch.running is False
    assert "npm install -g @larksuite/cli" in ch.error
    assert "cli_path" in ch.error
    # 状态里也要能看出是 CLI 缺失
    assert ch.status().extra["cli"] is False


async def test_feishu_text_is_split_for_long_replies(tmp_path, home):
    """超长回复要切分：飞书文本上限 150 KB，切分避免整条发失败。"""
    from skysheep.channels.feishu import FeishuChannel

    sent = []
    ch = FeishuChannel(
        {"app_id": "cli_x", "app_secret": "sec", "cli_path": _fake_cli(tmp_path)}, _noop_msg
    )
    real = ch._run_cli

    def spy(args, **kw):
        if "+messages-send" in args:
            sent.append(args)
        return real(args, **kw)

    ch._run_cli = spy
    ok = await ch.send_text("oc_1", "x" * 9000)
    assert ok is True
    assert len(sent) >= 3
    for args in sent:
        chunk = args[args.index("--text") + 1]
        assert len(chunk) <= 4000
        assert args[args.index("--as") + 1] == "bot"
        assert args[args.index("--chat-id") + 1] == "oc_1"


async def test_feishu_send_failure_is_reported(tmp_path, home):
    from skysheep.channels.feishu import FeishuChannel

    ch = FeishuChannel(
        {"app_id": "cli_x", "app_secret": "sec", "cli_path": _fake_cli(tmp_path, send_ok=False)},
        _noop_msg,
    )
    assert await ch.send_text("oc_1", "hi") is False
    assert "not in the chat" in ch.error


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
        self.actors = []
        self.chat_ids = []
        self.sessions = {}
        self.decisions = []
        self.chats = []
        self.store = None

    async def channel_ensure_session(self, name):
        return self.sessions.setdefault(name, f"sess-{name}")

    async def channel_new_session(self, name):
        self.sessions[name] = f"sess-{name}-new"
        return self.sessions[name]

    async def channel_run(self, session_id, text, actor="", chat_id=""):
        self.ran.append((session_id, text))
        self.actors.append((session_id, actor))
        self.chat_ids.append((session_id, chat_id))
        return {"text": "回复：" + text}

    async def channel_stop(self, session_id):
        self.stopped = session_id

    async def channel_status_text(self, session_id):
        return f"状态 {session_id}"

    async def channel_list_sessions(self):
        return [{"id": "s1", "title": "会话一"}]

    async def channel_submit_decision(self, name, decision, actor=""):
        self.decisions.append((name, decision, actor))
        if getattr(self, "actor_mismatch", False):
            return {"hit": False, "actor_mismatch": True}
        return {"hit": bool(getattr(self, "waiting", False))}

    def note_channel_chat(self, name, chat_id):
        self.chats.append((name, chat_id))


class _RecordingChannel(FeishuChannel):
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
    ch = _RecordingChannel(config.get("feishu") or {}, mgr._on_message)
    mgr.channels["feishu"] = ch
    return mgr, host, ch


async def test_manager_ignores_unapproved_source_silently():
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": []}})
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="你好", approved=False,
    ))
    # 关键：不回复。回复等于向陌生人确认机器人是活的。
    assert ch.out == []
    assert host.ran == []
    assert mgr.status()["channels"][0]["seen_sources"], "未授权来源应被记入待认领列表"


async def test_manager_runs_prompt_for_approved_source():
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="看下项目", approved=True,
    ))
    assert host.ran == [("sess-feishu", "看下项目")]
    assert ch.out and "回复：看下项目" in ch.out[0][1]
    assert host.chats == [("feishu", "9")]


async def test_manager_commands_do_not_hit_agent():
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="/status", approved=True,
    ))
    assert host.ran == []
    assert ch.out and "状态" in ch.out[0][1]

    ch.out.clear()
    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="/nope", approved=True,
    ))
    assert host.ran == []
    assert ch.out and "未知命令" in ch.out[0][1]


async def test_manager_routes_approval_reply_before_agent():
    """待审批时回 allow 必须是决定，不能当成新消息再跑一轮。"""
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    host.waiting = True
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="allow", approved=True,
    ))
    assert host.decisions == [("feishu", Decision.ALLOW_ONCE, "9")]
    assert host.ran == [], "审批回复不应触发新一轮 Agent 运行"

    # 没有待审批项时，allow 这种词应作为普通消息正常送进 Agent
    host.waiting = False
    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="allow", approved=True,
    ))
    assert host.ran == [("sess-feishu", "allow")]


async def test_manager_approval_rejected_for_non_initiator():
    """审批决定只认发起人：群聊里别人的 allow/deny 不生效，也不当新消息跑掉。"""
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["group-1"]}})
    host.waiting = True
    host.actor_mismatch = True
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="someone-else", chat_id="group-1", text="allow", approved=True,
    ))
    assert host.decisions == [("feishu", Decision.ALLOW_ONCE, "someone-else")]
    assert host.ran == []
    # 明确告知「不是发起人」，而不是装作无事发生
    assert ch.out and "发起" in ch.out[-1][1]


async def test_manager_passes_actor_to_run():
    """一轮的发起人要透传给宿主：后端拿它绑定审批决定。"""
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="u-77", chat_id="9", text="看下项目", approved=True,
    ))
    assert host.actors == [("sess-feishu", "u-77")]


async def test_manager_inbound_long_text_is_truncated():
    """入站超长文本截断并注明，不能整段灌进上下文（平台单条可到 150 KB）。"""
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage
    from skysheep.channels.manager import MAX_INBOUND_TEXT

    big = "啊" * (MAX_INBOUND_TEXT + 5000)
    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text=big, approved=True,
    ))
    sent = host.ran[0][1]
    assert len(sent) < len(big)
    assert sent.startswith("啊" * 100)
    assert "截断" in sent


async def test_manager_queues_message_with_ack():
    """一轮跑着时新消息排队并回执「已收到」，不能无声无息。"""
    import asyncio as _aio

    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    release = _aio.Event()

    async def slow_run(session_id, text, actor="", chat_id=""):
        host.ran.append((session_id, text))
        await release.wait()
        return {"text": "done"}

    host.channel_run = slow_run
    first = _aio.create_task(mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="第一条", approved=True,
    )))
    await _aio.sleep(0)  # 让第一条拿到锁、跑进 slow_run
    second = _aio.create_task(mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="第二条", approved=True,
    )))
    await _aio.sleep(0.05)
    # 第二条应先收到排队回执，而不是等到第一轮结束
    acks = [t for _, t in ch.out if "排队" in t]
    assert acks, "排队消息应收到回执"
    assert "前面还有 1 条" in acks[0]
    release.set()
    await _aio.gather(first, second)
    assert host.ran and host.ran[-1][1] == "第二条"


async def test_manager_rejects_when_queue_is_full():
    """排队超过上限直接拒收并告知：每条排队消息都是一整轮的 token。"""
    import asyncio as _aio

    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage
    from skysheep.channels.manager import MAX_PENDING_TURNS

    release = _aio.Event()

    async def slow_run(session_id, text, actor="", chat_id=""):
        host.ran.append((session_id, text))
        await release.wait()
        return {"text": "done"}

    host.channel_run = slow_run
    tasks = [_aio.create_task(mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text=f"m{i}", approved=True,
    ))) for i in range(MAX_PENDING_TURNS + 2)]
    # 等锁分配稳定：1 条在跑 + MAX_PENDING_TURNS 条排队，其余被拒收
    await _aio.sleep(0.1)
    release.set()
    await _aio.gather(*tasks)
    rejected = [t for _, t in ch.out if "没有执行" in t]
    assert len(rejected) == 1
    assert "排队的消息太多" in rejected[0]


async def test_manager_sends_busy_notice_for_slow_turn(monkeypatch):
    """一轮超过阈值没回音要先补一条「还在处理」；快轮不发（不打扰）。"""
    import asyncio as _aio

    import skysheep.channels.manager as manager_mod

    monkeypatch.setattr(manager_mod, "BUSY_NOTICE_DELAY", 0.05)
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    async def slow_run(session_id, text, actor="", chat_id=""):
        await _aio.sleep(0.2)
        return {"text": "done"}

    host.channel_run = slow_run
    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="慢任务", approved=True,
    ))
    assert any("还在处理" in t for _, t in ch.out)

    # 快轮：完成得比阈值早，不发提醒
    ch.out.clear()
    monkeypatch.setattr(manager_mod, "BUSY_NOTICE_DELAY", 30.0)
    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="快任务", approved=True,
    ))
    assert not any("还在处理" in t for _, t in ch.out)


async def test_manager_reports_run_error_back_to_chat():
    mgr, host, ch = _manager({"feishu": {"enabled": True, "allowed_ids": ["9"]}})
    from skysheep.channels.base import ChannelMessage

    async def boom(session_id, text, actor="", chat_id=""):
        raise RuntimeError("模型未配置")

    host.channel_run = boom
    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="9", chat_id="9", text="你好", approved=True,
    ))
    assert ch.out and "模型未配置" in ch.out[0][1]


async def test_manager_disabled_channel_is_not_started():
    mgr = ChannelManager(_FakeHost(), lambda: {"feishu": {"enabled": False, "app_id": "a"}})
    await mgr.restart()
    status = mgr.status()
    assert status["channels"][0]["running"] is False
    await mgr.stop()


async def test_manager_enabled_without_token_reports_error():
    mgr = ChannelManager(_FakeHost(), lambda: {"feishu": {"enabled": True, "app_id": "", "app_secret": ""}})
    await mgr.restart()
    item = mgr.status()["channels"][0]
    assert item["running"] is False
    # 错误文案是平台无关的兑底：不同平台缺的凭据字段不同，不能写死字段名
    assert "凭据" in item["error"]
    await mgr.stop()


async def test_onboarding_claim_flow_after_empty_allowlist_enable():
    """首次配置闭环：名单为空也能启用 → 陌生人消息只记进「发现的来源」→
    认领后同一条来源的消息才被真正处理。

    回归背景：启用曾被要求名单非空，而 chat id 只能由运行中的机器人发现，
    两个条件互斥，首次配置永远卡死（用户视角就是「渠道用不了」）。
    """
    from skysheep.channels.base import ChannelMessage

    config = {"feishu": {"enabled": True, "app_id": "a", "app_secret": "b", "allowed_ids": []}}
    mgr, host, ch = _manager(config)

    # 启用后机器人开始工作；第一条消息来自名单外 → 只记录，不回复、不执行
    await mgr._on_message(ChannelMessage(
        channel="feishu", actor="777", chat_id="777", text="你好", approved=False))
    assert ch.out == [] and host.ran == []
    seen = mgr.status()["channels"][0]["seen_sources"]
    assert [s["chat_id"] for s in seen] == ["777"]

    # 用户在界面上点「加入允许名单」→ 名单更新（模拟 channel.save 后的重建）
    config["feishu"]["allowed_ids"] = ["777"]
    mgr2, host2, ch2 = _manager(config)
    await mgr2._on_message(ChannelMessage(
        channel="feishu", actor="777", chat_id="777", text="看下项目", approved=True))
    assert host2.ran == [("sess-feishu", "看下项目")]
    assert ch2.out and ch2.out[0][0] == "777"


async def test_enabled_channel_with_empty_allowlist_stays_silent():
    """空名单运行中：陌生来源反复发消息也绝不回复、绝不执行（安全边界不因放开启用而松动）。"""
    from skysheep.channels.base import ChannelMessage

    mgr, host, ch = _manager(
        {"feishu": {"enabled": True, "app_id": "a", "app_secret": "b", "allowed_ids": []}}
    )
    for i in range(3):
        await mgr._on_message(ChannelMessage(
            channel="feishu", actor="e", chat_id="e", text=f"第{i}条", approved=False))
    assert ch.out == [] and host.ran == []
    seen = mgr.status()["channels"][0]["seen_sources"]
    assert len(seen) == 1 and seen[0]["count"] == 3


# ---------- 配置读写 ----------


def test_channels_config_defaults_and_roundtrip(home):
    from skysheep.config import load_config, update_config_section

    cfg = load_config()
    assert cfg.channels.platforms == {}
    assert cfg.channels.approve_timeout == 120

    update_config_section("channels", {"platforms": {"feishu": {
        "enabled": True, "app_id": "cli_x", "app_secret": "sec", "allowed_ids": ["1", "2"],
    }}})
    cfg2 = load_config()
    fs = cfg2.channels.platforms["feishu"]
    assert fs["enabled"] is True
    assert fs["app_id"] == "cli_x"
    assert fs["allowed_ids"] == ["1", "2"]


def test_channels_config_normalizes_numeric_ids(home):
    """TOML 里把 chat id 写成数字是常见笔误；不转成字符串会让名单静默失效。"""
    from skysheep.config import config_path, load_config

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[channels]\napprove_timeout = 60\n"
        "[channels.platforms.feishu]\nenabled = true\napp_id = \"a\"\napp_secret = \"b\"\n"
        "allowed_ids = [12345, 67890]\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.channels.approve_timeout == 60
    assert cfg.channels.platforms["feishu"]["allowed_ids"] == ["12345", "67890"]


def test_channels_config_tolerates_garbage(home):
    from skysheep.config import config_path, load_config

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[channels]\napprove_timeout = \"not-a-number\"\n"
        "[channels.platforms]\nfeishu = \"这不是配置\"\n",
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
        assert "feishu" in data["supported"]
        assert data["approve_timeout"] == 120


def test_channel_status_hides_unsupported_platforms(client, home):
    """升级后配置里残留的已下线平台（如旧 telegram 段）不应再渲染成卡片。

    它没有适配器，列出来就是一个永远启不动的死入口；但也不能去改用户配置文件。
    """
    from skysheep.config import update_config_section

    update_config_section("channels", {"platforms": {
        "telegram": {"enabled": True, "token": "stale", "allowed_ids": ["1"]},
    }})
    with client.websocket_connect("/ws") as ws:
        names = [c["name"] for c in _ws_call(ws, "c1", "channel.status")["result"]["channels"]]
    assert "telegram" not in names
    assert {"feishu", "weixin"} <= set(names)


def test_channel_enable_requires_token_and_allowlist(client, home):
    with client.websocket_connect("/ws") as ws:
        # 飞书的凭据是两个字段：先都不填
        frame = _ws_call(ws, "c1", "channel.enable", {"name": "feishu"})
        assert frame["ok"] is False
        assert "App ID" in frame["error"]
        # 只填 App ID 仍不能启用
        _ws_call(ws, "c2", "channel.save", {"name": "feishu", "app_id": "cli_x"})
        frame = _ws_call(ws, "c3", "channel.enable", {"name": "feishu"})
        assert frame["ok"] is False
        assert "App Secret" in frame["error"]


def test_channel_enable_with_empty_allowlist_starts_polling(client, home):
    """名单为空也允许启用：chat id 只能由运行中的机器人记进「发现的来源」，
    若启用时要求名单非空，首次配置就死锁（机器人不跑 → 永远收不到第一条消息）。
    安全语义不变：空名单 = 拒绝一切，由消息层强制，机器人对陌生人保持沉默。"""
    cli = _fake_cli(home, events=[])   # home 同时是 tmp_path，假 CLI 与配置都落在隔离目录
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "c1", "channel.save",
                 {"name": "feishu", "app_id": "a", "app_secret": "b", "cli_path": cli})
        frame = _ws_call(ws, "c2", "channel.enable", {"name": "feishu"})
        assert frame["ok"] is True
        fs = next(c for c in frame["result"]["channels"] if c["name"] == "feishu")
        assert fs["enabled"] is True
        assert fs["running"] is True, "启用后必须真的建立长连接，否则永远发现不了来源"
        assert fs["allowed_ids"] == []


def test_channel_enable_persists_and_disables(client, home):
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "c1", "channel.save", {
            "name": "feishu", "app_id": "cli_x", "app_secret": "sec",
            "allowed_ids": "12345\n67890",
        })
        frame = _ws_call(ws, "c2", "channel.enable", {"name": "feishu"})
        assert frame["ok"] is True
        fs = next(c for c in frame["result"]["channels"] if c["name"] == "feishu")
        assert fs["enabled"] is True
        assert fs["allowed_ids"] == ["12345", "67890"]
        # 凭据不回显，只回「已填」
        assert fs["has_app_id"] is True and fs["has_app_secret"] is True
        assert "app_secret" not in fs

        frame = _ws_call(ws, "c3", "channel.disable", {"name": "feishu"})
        fs = next(c for c in frame["result"]["channels"] if c["name"] == "feishu")
        assert fs["enabled"] is False


def test_channel_save_does_not_wipe_token_when_omitted(client, home):
    """界面保存允许名单时不该把已存的 Token 清掉。"""
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "c1", "channel.save", {"name": "feishu", "app_id": "cli_x", "app_secret": "sec"})
        frame = _ws_call(ws, "c2", "channel.save", {
            "name": "feishu", "allowed_ids": "999",
        })
        fs = next(c for c in frame["result"]["channels"] if c["name"] == "feishu")
        assert fs["has_app_id"] is True and fs["has_app_secret"] is True
        assert fs["allowed_ids"] == ["999"]


def test_channel_save_applies_allowlist_to_running_adapter(client, home):
    """保存名单必须重建适配器（热生效）：适配器拿的是构造时那份配置，
    不重建的话「加入允许名单」后机器人仍用启动时的旧名单判断，
    消息继续被忽略——名单存进去了，机器人却永远不回话。"""
    cli = _fake_cli(home, events=[])
    with client.websocket_connect("/ws") as ws:
        _ws_call(ws, "c1", "channel.save",
                 {"name": "feishu", "app_id": "cli_x", "app_secret": "sec",
                  "cli_path": cli})
        _ws_call(ws, "c2", "channel.enable", {"name": "feishu"})
        # 启用后（名单为空）再保存名单 —— 不点启用开关，只点保存
        frame = _ws_call(ws, "c3", "channel.save", {"name": "feishu", "allowed_ids": "ou_ok"})
        fs = next(c for c in frame["result"]["channels"] if c["name"] == "feishu")
        assert fs["enabled"] is True and fs["running"] is True
        # 重建后的适配器必须看到新名单：直接问正在跑的实例
        backend = client.app.state.backend
        ch = backend.channels.channels["feishu"]
        assert ch.is_allowed("ou_ok", "oc_any") is True
        assert ch.is_allowed("ou_stranger", "oc_other") is False


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
            sid = await be.channel_ensure_session("feishu")
            assert sid in be.runtimes
            # 模拟 runtime 丢失（重启后尚未重建）
            be.runtimes.clear()
            be._channel_gates.clear()
            be._channel_names.clear()

            # ensure 要能补建
            again = await be.channel_ensure_session("feishu")
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
            frame = _ws_call(ws, "c2", "channel.enable", {"name": "feishu"})
            assert frame["ok"] is False
            assert "桌面端" in frame["error"]


# ---------- 微信 iLink 渠道 ----------
# 协议形状取自腾讯官方 npm 包 @tencent-weixin/openclaw-weixin 的源码，
# 用 httpx.MockTransport 复刻响应，不打真实网络。


def _mock_transport(handler):
    return httpx.MockTransport(handler)


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
    """确认响应的凭据在**顶层**（官方包 login-qr.ts 的 StatusResponse 形状）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "get_qrcode_status" in str(request.url):
            return httpx.Response(200, json={
                "status": "confirmed", "ret": 0,
                "bot_token": "bt-1", "ilink_bot_id": "b1", "ilink_user_id": "u1",
                "baseurl": "https://ilinkai.weixin.qq.com",
            })
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({})
    ch._transport = _mock_transport(handler)
    st = await ch.poll_qrcode("qr")
    assert st["status"] == "confirmed"
    assert st["bot_token"] == "bt-1"
    assert st["base_url"] == "https://ilinkai.weixin.qq.com"
    await ch.stop()


async def test_weixin_login_confirmed_credentials_fallback_still_works():
    """官方文档页的 credentials 包一层是旧形状；顶层没有时按兜底取。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "get_qrcode_status" in str(request.url):
            return httpx.Response(200, json={
                "status": "confirmed", "ret": 0,
                "credentials": {"bot_token": "bt-2"},
            })
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({})
    ch._transport = _mock_transport(handler)
    st = await ch.poll_qrcode("qr")
    assert st["status"] == "confirmed"
    assert st["bot_token"] == "bt-2"
    await ch.stop()


async def test_weixin_login_confirmed_without_token_returns_no_token():
    """确认了却没有凭据：结果里不带 bot_token（结构已记日志），由宿主给出报错。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "get_qrcode_status" in str(request.url):
            return httpx.Response(200, json={"status": "confirmed", "ret": 0})
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({})
    ch._transport = _mock_transport(handler)
    st = await ch.poll_qrcode("qr")
    assert st["status"] == "confirmed"
    assert "bot_token" not in st
    await ch.stop()


async def test_weixin_poll_redirect_switches_host():
    """scaned_but_redirect（IDC 迁移）：轮询主机要换到 redirect_host 再继续。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "get_qrcode_status" in str(request.url):
            return httpx.Response(200, json={
                "status": "scaned_but_redirect", "ret": 0,
                "redirect_host": "il-hop.weixin.qq.com",
            })
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({})
    ch._transport = _mock_transport(handler)
    st = await ch.poll_qrcode("qr")
    assert st == {"status": "scaned"}
    assert ch.base_url == "https://il-hop.weixin.qq.com"
    await ch.stop()


async def test_weixin_poll_binded_and_verify_code_roundtrip():
    """binded_redirect 映射为 binded；need_verifycode 时 verify_code 要带上重试。"""
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen_urls.append(url)
        if "get_qrcode_status" in url:
            if "verify_code=1234" in url:
                return httpx.Response(200, json={"status": "scaned", "ret": 0})
            return httpx.Response(200, json={"status": "need_verifycode", "ret": 0})
        return httpx.Response(200, json={"ret": 0})

    ch = _wx({})
    ch._transport = _mock_transport(handler)
    st = await ch.poll_qrcode("qr")
    assert st["status"] == "need_verifycode"
    st = await ch.poll_qrcode("qr", verify_code=" 1234 ")  # 带空白也要剥掉再编码
    assert st["status"] == "scaned"
    assert any("verify_code=1234" in u for u in seen_urls)

    def binded_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "binded_redirect", "ret": 0})

    ch2 = _wx({})
    ch2._transport = _mock_transport(binded_handler)
    st2 = await ch2.poll_qrcode("qr")
    assert st2 == {"status": "binded"}
    await ch.stop()
    await ch2.stop()


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
    两者不一致时微信的回复会落到飞书的会话里（/status 显示错平台）。
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
        fs = _RecordingChannel({"app_id": "a", "app_secret": "b", "allowed_ids": ["t1"]}, mgr._on_message)
        wx = _RecordingChannel({"bot_token": "k", "allowed_ids": ["w1"]}, mgr._on_message)
        fs.name = "feishu"
        wx.name = "weixin"
        mgr.channels["feishu"] = fs
        mgr.channels["weixin"] = wx

        await mgr._on_message(ChannelMessage(
            channel="weixin", actor="w1", chat_id="w1", text="你好", approved=True))
        await mgr._on_message(ChannelMessage(
            channel="feishu", actor="t1", chat_id="t1", text="你好", approved=True))

        sid_wx = await be.store.get_channel_binding("weixin")
        sid_fs = await be.store.get_channel_binding("feishu")
        assert sid_wx and sid_fs
        assert sid_wx != sid_fs, "两个渠道必须是独立会话"

        sess_wx = await be.store.get_session(sid_wx)
        sess_fs = await be.store.get_session(sid_fs)
        assert "weixin" in sess_wx.title
        assert "feishu" in sess_fs.title
    finally:
        await be.shutdown()


async def test_manager_sets_adapter_name_from_registry_key():
    """注册键要写回适配器实例，让事件里的 channel 字段与字典键一致。"""
    from skysheep.channels.manager import ChannelManager

    mgr = ChannelManager(_FakeHost(), lambda: {"feishu": {"enabled": False, "app_id": "a"}})
    await mgr.restart()
    ch = mgr.channels["feishu"]
    assert ch.name == "feishu"
    await mgr.stop()


def test_channel_sessions_live_in_fixed_remote_project(home):
    """渠道会话固定归到「远程连接」项目：不随当前项目走、项目不可删、归属校验放行。

    「远程连接」是哨兵项目（无真实目录）：channel_ensure_session / channel_new_session
    建的会话都挂它名下；桌面端按 id 打开（_get_owned_session）时要像快聊一样放行，
    否则侧栏里看得见点不动；project.delete 必须拒绝删除这个固定项目。
    """
    import asyncio

    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider
    from skysheep.server.backend import ServerBackend
    from skysheep.session.store import SessionStore

    async def scenario():
        be = ServerBackend(
            working_dir=home / "proj",
            provider_name="fake",
            provider_factory=lambda: FakeProvider([]).with_default([TextBlock(text="好")]),
        )
        await be.setup()
        try:
            # 启动目录会自动登记为当前项目：渠道会话仍归「远程连接」，不落当前项目
            assert be.project is not None
            sid = await be.channel_ensure_session("feishu")
            sess = await be.store.get_session(sid)
            remote = await be.store.ensure_remote_project()
            assert sess.project_id == remote.id, "渠道会话必须挂在「远程连接」下"
            assert sess.project_id != be.project.id, "不能挂在桌面当前项目下"

            # 幂等：重复 ensure 只有一条「远程连接」记录
            again = await be.store.ensure_remote_project()
            assert again.id == remote.id
            all_projects = await be.store.list_projects()
            assert sum(1 for p in all_projects if p.root_path == SessionStore.REMOTE_PROJECT_PATH) == 1

            # 渠道 /new 也归「远程连接」，即使桌面正开着别的（当前）项目
            new_sid = await be.channel_new_session("feishu")
            new_sess = await be.store.get_session(new_sid)
            assert new_sess.project_id == remote.id
            assert new_sid != sid

            # 桌面端按 id 打开渠道会话（当前项目是启动目录，会话属于远程项目）：放行
            owned = await be._get_owned_session(sid)
            assert owned.id == sid

            # 固定项目不可删除
            try:
                await be.delete_project(remote.id)
                raise AssertionError("删除「远程连接」应当被拒绝")
            except RuntimeError as e:
                assert "远程连接" in str(e)

            # 渠道自愈的归属校验认「远程连接」：runtime 丢了也能补建并跑通
            be.runtimes.clear()
            be._channel_names.clear()
            result = await be.channel_run(new_sid, "你好")
            assert "error" not in result, result
        finally:
            await be.shutdown()

    asyncio.run(scenario())


def test_channel_allowed_tools_warning():
    """渠道预授权写/执行类工具时给出明确告警（无人值守 = 任意命令）。"""
    from skysheep.server.backend import _channel_allowed_tools_warning

    assert _channel_allowed_tools_warning([]) == ""
    assert _channel_allowed_tools_warning(["read_file", "glob"]) == ""
    msg = _channel_allowed_tools_warning(["read_file", "run_command", "write_file"])
    assert "run_command" in msg and "write_file" in msg
    assert "无人值守" in msg
