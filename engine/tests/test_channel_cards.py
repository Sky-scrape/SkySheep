"""渠道富卡片（P21）：卡片构建器形状、文本降级、飞书参数装配与推送接线。

lark-cli 的 ``--msg-type interactive --content`` 参数名镜像 Open API 语义；
本机没有配置飞书渠道时只验证参数装配与降级路径，真实卡片渲染需在配置好
渠道的机器上发一条实测（调用方失败自动降级纯文本，最坏不劣于旧行为）。
"""

from __future__ import annotations

import asyncio
import json

from skysheep.channels.base import Channel
from skysheep.channels.cards import (
    card_to_json,
    card_to_text,
    permission_request_card,
    task_finish_card,
)
from skysheep.server.backend_parts.automation import push_text_to_targets

# ---- 构建器形状 ----


def test_task_finish_card_shape():
    card = task_finish_card(name="定时任务 备份", lines=["状态：完成"], ok=True)
    assert card["header"]["template"] == "green"
    assert "备份" in card["header"]["title"]["content"]
    assert card["elements"][0]["tag"] == "markdown"
    assert "状态：完成" in card["elements"][0]["content"]
    bad = task_finish_card(name="x", lines=["失败明细"], ok=False)
    assert bad["header"]["template"] == "orange"
    # 空行列表兜底：不产出空正文卡
    assert task_finish_card(name="x", lines=["", "  "], ok=True)["elements"][0]["content"] == "（无详情）"


def test_permission_request_card_never_allows_inline_action():
    """权限卡安全口径：除「打开应用处理」跳转外不得出现任何放行/拒绝按钮。"""
    card = permission_request_card(
        title="run_command 请求执行",
        detail="命令：npm test\n项目：demo",
        link="skysheep://focus",
    )
    actions = [a for el in card["elements"] if el.get("tag") == "action" for a in el["actions"]]
    assert len(actions) == 1, "权限卡只允许一个跳转按钮"
    assert actions[0]["url"] == "skysheep://focus"
    assert actions[0]["text"]["content"] == "打开 SkySheep 处理"
    text = json.dumps(card, ensure_ascii=False)
    for forbidden in ("放行", "允许", "拒绝", "确认执行"):
        assert forbidden not in text, f"权限卡不得内嵌「{forbidden}」动作文案"
    # 没注册协议（浏览器兜底）时省略按钮，只留说明
    bare = permission_request_card(title="t", detail="d", link=None)
    assert not [el for el in bare["elements"] if el.get("tag") == "action"]


def test_card_to_text_extracts_all_elements():
    card = permission_request_card(title="写文件请求", detail="路径：a.txt", link="skysheep://focus")
    text = card_to_text(card)
    assert "🔐 权限请求" in text
    assert "写文件请求" in text
    assert "路径：a.txt" in text
    assert text.count("> ") >= 1  # note 降级为引用行
    assert "[打开 SkySheep 处理] skysheep://focus" in text


def test_card_to_json_compact_and_unescaped():
    card = task_finish_card(name="任务「中文」", lines=["行"], ok=True)
    raw = card_to_json(card)
    assert "中文" in raw  # ensure_ascii=False
    assert "\\u" not in raw
    assert json.loads(raw) == card


# ---- 降级契约与飞书参数装配 ----


class _TextOnly(Channel):
    """不覆写 send_card 的渠道：走基类默认降级（卡抽成纯文本）。"""

    name = "textonly"

    def __init__(self):
        super().__init__({"enabled": True}, None)
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None:
        return None

    async def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _FakeFeishu(Channel):
    """覆写 send_card 的渠道：记录调用并按预设返回成败。"""

    name = "fakefeishu"

    def __init__(self, ok=True):
        super().__init__({"enabled": True, "allowed_ids": ["oc_1"]}, None)
        self.cards: list[tuple[str, dict]] = []
        self.texts: list[tuple[str, str]] = []
        self._ok = ok

    async def start(self) -> None:
        return None

    async def send_card(self, chat_id, card):
        self.cards.append((chat_id, card))
        return self._ok

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))
        return True


def _run(coro):
    return asyncio.run(coro)


def test_base_channel_send_card_falls_back_to_text():
    ch = _TextOnly()
    card = task_finish_card(name="t", lines=["l"], ok=True)
    assert _run(ch.send_card("oc_x", card)) is True
    assert ch.sent and ch.sent[0][0] == "oc_x"
    assert "t" in ch.sent[0][1] and "l" in ch.sent[0][1]


def test_feishu_send_card_args(tmp_path, monkeypatch):
    """send_card 装配 lark-cli 参数：--msg-type interactive --content <紧凑JSON>。"""
    from skysheep.channels.feishu import FeishuChannel

    ch = FeishuChannel.__new__(FeishuChannel)  # 绕过 start 流程，只测装配
    ch.cli = "lark-cli"
    ch.error = ""
    captured = {}

    def fake_run(args, timeout=None):
        captured["args"] = args
        return True, {"ok": True}

    monkeypatch.setattr(ch, "_run_cli", fake_run)
    card = task_finish_card(name="n", lines=["l"], ok=True)
    assert _run(ch.send_card("oc_9", card)) is True
    args = captured["args"]
    assert args[:4] == ["im", "+messages-send", "--as", "bot"]
    assert "--chat-id" in args and args[args.index("--chat-id") + 1] == "oc_9"
    assert args[args.index("--msg-type") + 1] == "interactive"
    content = args[args.index("--content") + 1]
    assert json.loads(content) == card
    # 失败路径：返回 False 并落 error，不抛
    monkeypatch.setattr(ch, "_run_cli", lambda args, timeout=None: (False, {"error": {"message": "bad"}}))
    assert _run(ch.send_card("oc_9", card)) is False
    assert "bad" in ch.error


def test_feishu_send_card_guard_without_cli():
    from skysheep.channels.feishu import FeishuChannel

    ch = FeishuChannel.__new__(FeishuChannel)
    ch.cli = None
    ch.error = ""
    assert _run(ch.send_card("oc_1", {"a": 1})) is False


# ---- 推送接线 ----


class _Mgr:
    def __init__(self, channels):
        self.channels = {c.name: c for c in channels}


def test_push_with_card_native_success_no_text_dup():
    feishu = _FakeFeishu(ok=True)
    card = task_finish_card(name="n", lines=["l"], ok=True)
    n = _run(push_text_to_targets(_Mgr([feishu]), "文本正文", card=card))
    assert n == 1
    assert len(feishu.cards) == 1 and feishu.cards[0][0] == "oc_1"
    assert feishu.texts == [], "卡片成功后不应再发纯文本（防重复）"


def test_push_card_failure_falls_back_to_text_once():
    feishu = _FakeFeishu(ok=False)
    card = task_finish_card(name="n", lines=["l"], ok=False)
    n = _run(push_text_to_targets(_Mgr([feishu]), "文本正文", card=card))
    assert n == 1
    assert feishu.cards == [("oc_1", card)]
    assert feishu.texts == [("oc_1", "文本正文")], "卡片失败恰好降级一次文本"


def test_push_card_to_text_only_channel_and_webhook():
    """不支持卡片的渠道走基类降级；webhook 纯出站整段一张。"""
    text_only = _TextOnly()
    text_only.config["allowed_ids"] = ["oc_2"]
    webhook = _TextOnly()
    webhook.pure_outbound = True
    webhook.name = "hook"
    card = task_finish_card(name="n", lines=["l"], ok=True)
    n = _run(push_text_to_targets(_Mgr([text_only, webhook]), "文本正文", card=card))
    assert n == 2
    assert text_only.sent and "文本" not in text_only.sent[0][1]  # 降级发的是卡片文本
    assert webhook.sent and webhook.sent[0][0] == ""


def test_push_without_card_unchanged():
    """card=None（既有调用方）行为完全不变：只发纯文本。"""
    feishu = _FakeFeishu(ok=True)
    n = _run(push_text_to_targets(_Mgr([feishu]), "纯文本"))
    assert n == 1
    assert feishu.cards == [] and feishu.texts == [("oc_1", "纯文本")]


# ---- 权限请求推渠道（P21 剩余半边：推送开关 + 接线）----


class _Store:
    """只实现 get_session_title 的假 store（权限提醒只需标题）。"""

    def __init__(self, title="", boom=False):
        self.title = title
        self.boom = boom

    async def get_session_title(self, sid):  # pragma: no cover - 形状即契约
        if self.boom:
            raise RuntimeError("db gone")
        return self.title


def test_permission_push_body_and_headline():
    from skysheep.server.backend_parts.channels import (
        permission_push_body,
        permission_push_headline,
    )

    ev = {
        "tool_name": "run_command",
        "detail": "命令：npm test\n项目：demo",
        "scope": "工作目录内路径",
        "origin": "团队队员 · 前端",
        "session_id": "s1",
    }
    assert permission_push_headline(ev) == "run_command 请求执行"
    body = permission_push_body(ev, "重构会话")
    assert "会话：重构会话" in body
    assert "操作：命令：npm test" in body
    assert "影响范围：工作目录内路径" in body
    assert "发起方：团队队员 · 前端" in body
    assert "超时未回复按拒绝处理" in body
    # 没有标题退回会话 id，再退回占位文案
    assert "会话：s1" in permission_push_body(ev, "")
    assert "（未命名会话）" in permission_push_body({"tool_name": "t"}, "")
    # 超长 detail 截断，不当成渠道消息塞满屏幕
    long_body = permission_push_body({"tool_name": "write_file", "detail": "x" * 2000}, "t")
    assert "…（已截断）" in long_body
    assert len(long_body) < 800


def test_push_permission_request_reminds_without_approval_surface():
    """权限提醒卡：只有唤回跳转，没有任何放行/拒绝入口；标题取自 store。"""
    from skysheep.server.backend_parts.channels import push_permission_request

    feishu = _FakeFeishu(ok=True)
    ev = {"tool_name": "run_command", "detail": "命令：npm test", "session_id": "s1"}
    n = _run(push_permission_request(_Mgr([feishu]), _Store("重构会话"), ev))
    assert n == 1
    assert len(feishu.cards) == 1 and feishu.texts == []
    card = feishu.cards[0][1]
    actions = [a for el in card["elements"] if el.get("tag") == "action" for a in el["actions"]]
    assert len(actions) == 1 and actions[0]["url"] == "skysheep://focus"
    assert "run_command 请求执行" in json.dumps(card, ensure_ascii=False)
    assert "重构会话" in json.dumps(card, ensure_ascii=False)
    # 提醒失败不能影响权限流程：标题查不到也照发（退回会话 id）
    boom = _FakeFeishu(ok=True)
    n2 = _run(push_permission_request(_Mgr([boom]), _Store(boom=True), ev))
    assert n2 == 1
    assert "会话：s1" in json.dumps(boom.cards[0][1], ensure_ascii=False)


def test_permission_push_setting_defaults_off_and_roundtrips(home):
    """开关默认关，写成 true 才生效；非布尔值一律当关（宁可漏提醒不可误开）。"""
    from skysheep.config import load_config, update_config_section

    assert load_config().channels.permission_push is False
    update_config_section("channels", {"permission_push": True})
    assert load_config().channels.permission_push is True
    update_config_section("channels", {"permission_push": "yes"})  # 非 true 字面量
    assert load_config().channels.permission_push is False


def test_fire_permission_push_respects_switch():
    """接线门：开关关不推；开着才推，且不阻塞调用方（fire-and-forget）。"""
    import types

    from skysheep.server.backend_parts.channels import ChannelsMixin

    class _Backend(ChannelsMixin):
        pass

    off = _Backend()
    off.cfg = types.SimpleNamespace(channels=types.SimpleNamespace(permission_push=False))
    feishu = _FakeFeishu(ok=True)
    off.channels = _Mgr([feishu])
    off.store = _Store("会话A")
    ev = {"tool_name": "run_command", "detail": "命令：ls", "session_id": "s1"}

    async def go(backend):
        backend._fire_permission_push(ev)
        await asyncio.sleep(0.05)  # 给后台任务一个跑完的机会
        return feishu

    _run(go(off))
    assert feishu.cards == [], "开关关时不得推送"

    on = _Backend()
    on.cfg = types.SimpleNamespace(channels=types.SimpleNamespace(permission_push=True))
    on.channels = _Mgr([feishu])
    on.store = _Store("会话A")
    _run(go(on))
    assert len(feishu.cards) == 1
