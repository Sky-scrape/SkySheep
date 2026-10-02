"""通用 Webhook 渠道：纯出站 POST、HMAC-SHA256 签名、不可达静默失败、推送目标语义。

测试用本机临时 HTTP server 真实收包（不走 mock transport），对「出站 JSON 的
形状 / 签名头 / 失败静默」做端到端断言；推送目标口径（纯出站渠道 allowed_ids
留空即广播、聊天渠道空名单仍拒发）直接测 automation 的目标判定。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skysheep.channels.manager import ChannelManager, build_channels
from skysheep.channels.webhook import SIGNATURE_HEADER, WebhookChannel
from skysheep.server.backend_parts.automation import (
    _cron_push_targets_of,
    push_text_to_targets,
)

# ---- 本机临时 HTTP server：收包断言的接收端 ----


class _Collector:
    """127.0.0.1 上的临时 HTTP server：把收到的每个 POST 记下来。"""

    def __init__(self, status: int = 200):
        self.requests: list[dict] = []
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({
                    "path": self.path,
                    "body": self.rfile.read(length),
                    # httpx 在线上把头名统一成小写：收包侧也按小写键存，查头不再猜大小写
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *_args):  # 静音默认访问日志
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/hook"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector():
    c = _Collector()
    yield c
    c.close()


def _channel(collector_: _Collector, **extra) -> WebhookChannel:
    return WebhookChannel({"enabled": True, "url": collector_.url, **extra}, lambda _m: None)


# ---- 适配器本体 ----


async def test_send_posts_json_to_configured_url(collector):
    ch = _channel(collector)
    assert ch.configured() is True
    assert await ch.send_text("oc_chat", "你好，webhook") is True
    assert ch.error == ""
    req = collector.requests[0]
    data = json.loads(req["body"])
    assert data["channel"] == "webhook"
    assert data["chat_id"] == "oc_chat"
    assert data["text"] == "你好，webhook"
    assert data["sent_at"]  # 时间戳字段存在即可，格式不锁死
    assert req["headers"]["content-type"] == "application/json"
    # 没配 secret 就不该有签名头
    assert SIGNATURE_HEADER.lower() not in req["headers"]


async def test_signature_is_hmac_sha256_over_raw_body(collector):
    ch = _channel(collector, secret="s3cret")
    assert await ch.send_text("", "签名我") is True
    req = collector.requests[0]
    expect = "sha256=" + hmac.new(b"s3cret", req["body"], hashlib.sha256).hexdigest()
    assert req["headers"].get(SIGNATURE_HEADER.lower()) == expect


async def test_long_text_is_not_split(collector):
    """webhook 无平台限长：长文本整段一条 POST（切分会破坏结构化消费）。"""
    ch = _channel(collector)
    long_text = "长" * 20000
    assert await ch.send_text("", long_text) is True
    assert len(collector.requests) == 1
    assert json.loads(collector.requests[0]["body"])["text"] == long_text


async def test_unreachable_url_fails_silently():
    """URL 不可达：返回 False、不抛、原因留在 error（端口 1 不可能有监听）。"""
    ch = WebhookChannel({"enabled": True, "url": "http://127.0.0.1:1/nope"}, lambda _m: None)
    ok = await ch.send_text("", "在吗")
    assert ok is False
    assert ch.error  # 原因要可读，但调用方拿 False 自己决定怎么处理


async def test_non_2xx_response_is_failure():
    c = _Collector(status=500)
    try:
        ch = _channel(c)
        assert await ch.send_text("", "hello") is False
        assert "500" in ch.error
        assert len(c.requests) == 1
    finally:
        c.close()


async def test_without_url_is_not_configured_and_send_refuses():
    ch = WebhookChannel({"enabled": True}, lambda _m: None)
    assert ch.configured() is False
    assert await ch.send_text("", "x") is False


async def test_status_extra_hides_full_url(collector):
    """状态补充里只给 host，不回显完整 URL（query 里可能带 token）。"""
    ch = _channel(collector, secret="k")
    st = ch.status()
    assert st.extra["has_secret"] is True
    assert collector.url not in str(st.extra["url_host"])
    assert st.extra["url_host"]  # host 部分仍在（设置页展示用）


async def test_pure_outbound_lifecycle_has_no_background_task():
    """纯出站：start 无事可做、running 恒为 False、stop 干净退出。"""
    ch = WebhookChannel({"enabled": True, "url": "http://example.com/hook"}, lambda _m: None)
    await ch.start()
    assert ch.running is False
    await ch.stop()
    assert ch._client is None


# ---- manager 装配 ----


def test_webhook_registered_in_build_channels():
    built = build_channels(
        {"webhook": {"enabled": False, "url": "http://example.com/hook"}},
        lambda _m: None,
    )
    ch = built["webhook"]
    assert isinstance(ch, WebhookChannel)
    assert ch.name == "webhook"  # 注册键写回实例（与飞书/微信同款口径）


async def test_manager_restarts_webhook_without_error():
    cfg = {"webhook": {"enabled": True, "url": "http://example.com/hook"}}
    mgr = ChannelManager(None, lambda: cfg)
    try:
        st = await mgr.restart()
        entry = next(c for c in st["channels"] if c["name"] == "webhook")
        assert entry["enabled"] is True and entry["configured"] is True
        assert entry["error"] == ""
        assert "webhook" in st["supported"]
    finally:
        await mgr.stop()


async def test_manager_reports_incomplete_webhook_config():
    cfg = {"webhook": {"enabled": True}}  # 没填 URL 就启用
    mgr = ChannelManager(None, lambda: cfg)
    try:
        st = await mgr.restart()
        entry = next(c for c in st["channels"] if c["name"] == "webhook")
        assert entry["configured"] is False and entry["error"]
    finally:
        await mgr.stop()


# ---- 推送目标语义（与既有契约的调和点） ----


class _ChatLike:
    """聊天渠道形状的替身：**不设** pure_outbound 属性（getattr 缺省必须兜住）。"""

    name = "feishu"

    def __init__(self, allowed=(), *, enabled=True):
        self.config = {"enabled": enabled, "allowed_ids": list(allowed)}
        self.sent: list[tuple[str, str]] = []

    def configured(self) -> bool:
        return True

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    @property
    def allowed_ids(self) -> set[str]:
        return {str(x) for x in self.config.get("allowed_ids") or []}

    async def send_text(self, chat_id: str, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True


class _Mgr:
    def __init__(self, **channels):
        self.channels = channels


async def test_push_targets_webhook_broadcasts_without_allowlist(collector):
    """核心语义调和：webhook 空 allowed_ids = 广播；聊天渠道空名单仍然拒发。"""
    hook = _channel(collector)
    chat_empty = _ChatLike([])          # 聊天渠道空名单：排除
    chat_bound = _ChatLike(["a", "b"])  # 聊天渠道有名单：逐 chat_id 推
    chat_off = _ChatLike(["c"], enabled=False)
    hook_unconfigured = WebhookChannel({"enabled": True}, lambda _m: None)  # 没有 URL：排除
    mgr = _Mgr(webhook=hook, feishu_empty=chat_empty, feishu_ok=chat_bound,
               feishu_off=chat_off, webhook_bad=hook_unconfigured)

    targets = _cron_push_targets_of(mgr)
    assert targets == [hook, chat_bound]

    delivered = await push_text_to_targets(mgr, "推送一次", what="测试")
    assert delivered == 3  # webhook 1 次 + 聊天渠道 2 个 chat_id
    assert len(collector.requests) == 1
    assert json.loads(collector.requests[0]["body"])["text"] == "推送一次"
    assert sorted(cid for cid, _ in chat_bound.sent) == ["a", "b"]
    assert chat_empty.sent == [] and chat_off.sent == []


async def test_push_to_unreachable_webhook_does_not_raise():
    """端点不可达：推送静默失败（记日志），绝不把异常冒回调用方。"""

    class _BrokenHook(WebhookChannel):
        async def send_text(self, chat_id, text):
            raise RuntimeError("网络炸了")

    mgr = _Mgr(webhook=_BrokenHook({"enabled": True, "url": "http://x"}, lambda _m: None))
    assert await push_text_to_targets(mgr, "正文", what="测试") == 0


def test_pure_outbound_flag_declared_on_base():
    """契约显式落在基类：聊天渠道默认非纯出站，语义由 automation 统一口径。"""
    from skysheep.channels.base import Channel

    assert Channel.pure_outbound is False
    assert WebhookChannel.pure_outbound is True


# ---- 后端渠道设置面（配置存既有渠道配置路径） ----


def test_credentials_ready_for_webhook_requires_url():
    from skysheep.server.backend_parts.channels import ChannelsMixin

    ready = ChannelsMixin._channel_credentials_ready
    assert ready(None, "webhook", {"url": "http://x"}) is True
    assert ready(None, "webhook", {"url": ""}) is False
    assert ready(None, "webhook", {}) is False
    # 其它平台不受影响（历史口径：单 token）
    assert ready(None, "other", {"token": "t"}) is True


# ---- 端到端：每日运行日报经 webhook 渠道真实送达（B 的推送目标复用 A 的适配器） ----


async def test_daily_report_delivered_through_webhook_channel(store, home, collector):
    from skysheep.server.backend_parts import daily_report as dr
    from skysheep.server.backend_parts.daily_report import (
        load_state,
        run_daily_report_pass,
        save_state,
    )

    # 一次当天的失败运行 → 日报应有失败明细
    start, _end = dr._day_bounds(time.time())
    now = start + 3600
    project = await store.get_or_create_project("/tmp/daily-proj")
    t = await store.add_cron_task(project.id, "抓行情", "干活", "interval", interval_minutes=30)
    await store.update_cron_task(
        t["id"], last_run_at=now, last_status="error", last_result="连接超时",
    )

    ch = WebhookChannel({"enabled": True, "url": collector.url, "secret": "k"},
                        lambda _m: None)
    assert _cron_push_targets_of(_Mgr(webhook=ch)) == [ch]

    save_state({"enabled": True, "time": "00:00"})
    out = await run_daily_report_pass(store, _Mgr(webhook=ch), now=now)
    assert out["sent"] is True
    # 真实 HTTP server 收到了带签名的日报 POST
    req = collector.requests[0]
    body = json.loads(req["body"])
    assert "运行日报" in body["text"] and "定时任务：成功 0 / 失败 1" in body["text"]
    assert "· 定时任务「抓行情」失败：连接超时" in body["text"]
    expect = "sha256=" + hmac.new(b"k", req["body"], hashlib.sha256).hexdigest()
    assert req["headers"].get(SIGNATURE_HEADER.lower()) == expect
    # 防重发日期已记，同一天第二遍不再发
    assert load_state()["last_sent_date"] == dr.today_str(now)
    out2 = await run_daily_report_pass(store, _Mgr(webhook=ch), now=now + 60)
    assert out2["reason"] == "already_sent"
    assert len(collector.requests) == 1
