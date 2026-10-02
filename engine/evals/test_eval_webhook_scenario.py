"""评测基线 · 渠道 Webhook 出站场景（真实 WebhookChannel + mock HTTP 传输）。

场景 30（并行流的通用 Webhook 渠道已落地到当前工作树：channels/webhook.py）：
钉纯出站渠道的推送开关语义——定时任务终态摘要经真实的
_cron_push_targets_of → push_text_to_targets 出站：

- enabled=False（默认）：configured 也不发，零 POST；
- enabled=True + configured（url 已填）：整段 POST 一次（纯出站没有 chat_id
  维度，allowed_ids 留空代表广播——与聊天渠道「空名单拒发一切」语义相反）；
- 请求形状固定：POST JSON {channel, chat_id, text, sent_at}；配置了 secret
  时带 X-SkySheep-Signature: sha256=<HMAC-SHA256(原始请求体)>（GitHub
  webhook 同款），接收方可验签；
- 推送是附赠动作：POST 与否不影响任务行回写。

HTTP 层用 httpx.MockTransport 拦截（webhook 的文档化测试注入点 _transport），
不触任何网络；编排执行主体与场景 24/28 同一套真实后端路径。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx

from evals.test_eval_pipeline_scenarios import (
    _make_backend,
    _RoutedProvider,
    _wait_until,
)
from skysheep.channels.webhook import SIGNATURE_HEADER, WebhookChannel


def _recording_transport(posts: list[httpx.Request]):
    """记录 POST 的 mock 传输层：200 回复，绝不触网。"""

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200)

    return httpx.MockTransport(handler)


async def test_eval_webhook_outbound_switch_semantics(home):
    """场景 30（Webhook 出站开关 · 真实渠道 + mock 传输）：enabled 门与请求形状。

    同一条出站链路（定时任务终态推送）两种开关状态：
    - 关：任务照跑、任务行照常回写，webhook 零请求；
    - 开：整段 POST 一次，JSON 形状固定，secret 出 HMAC 签名头且可用同一
      secret 重算验证；同轮绑定的聊天渠道照常走名单投递（两种渠道目标口径
      并行不悖）。
    """
    provider = _RoutedProvider([])
    provider.ROUTES = {"汇总今日进展": "今日三件事都完成了。"}
    be = await _make_backend(home, provider)
    posts: list[httpx.Request] = []
    hook = WebhookChannel(
        {"enabled": False, "url": "http://hook.local/endpoint", "secret": "s3cret"},
        on_message=None,
    )
    hook._transport = _recording_transport(posts)  # webhook 的文档化测试注入点
    be.channels.channels["webhook"] = hook
    try:
        pid = be.project.id
        t_off = await be.store.add_cron_task(
            pid, "关开关任务", "汇总今日进展", "interval", 30,
            allowed_tools=[], notify_channel=True)
        t_on = await be.store.add_cron_task(
            pid, "开开关任务", "汇总今日进展", "interval", 30,
            allowed_tools=[], notify_channel=True)

        # 开关关（enabled=False）：configured 也不发，零请求；任务行照常回写
        await be._run_cron_task(t_off["id"], force=True)
        await asyncio.sleep(0.3)  # 留出「不该发的多发了」的暴露窗口
        assert posts == [], f"webhook 未启用不得发出任何 POST，实际 {posts}"
        row = await be.store.get_cron_task(t_off["id"])
        assert row["last_status"] == "ok", "推送与否不影响任务行回写"

        # 开关开：整段 POST 一次，形状与签名可验
        hook.config["enabled"] = True
        await be._run_cron_task(t_on["id"], force=True)
        assert await _wait_until(lambda: len(posts) >= 1), "启用后应 POST 一次"
        await asyncio.sleep(0.3)
        assert len(posts) == 1, f"纯出站没有 chat 维度：一条摘要恰好一次 POST，实际 {posts}"
        req = posts[0]
        assert req.method == "POST" and str(req.url) == "http://hook.local/endpoint"
        body = json.loads(req.content.decode("utf-8"))
        assert body["channel"] == "webhook" and body["chat_id"] == ""
        assert "开开关任务" in body["text"] and "今日三件事都完成了。" in body["text"]
        assert body["sent_at"], "带发送时间戳"
        assert req.headers["user-agent"].startswith("SkySheep-Webhook")
        expected_sig = "sha256=" + hmac.new(
            b"s3cret", req.content, hashlib.sha256).hexdigest()
        assert req.headers.get(SIGNATURE_HEADER) == expected_sig, (
            "签名 = 对原始请求体的 HMAC-SHA256，接收方可用同一 secret 验证"
        )
    finally:
        await be.shutdown()
