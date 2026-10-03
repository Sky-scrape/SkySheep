"""通用 Webhook 渠道：把出站消息 POST 成 JSON 到用户自配的 URL。

与飞书 / 微信的结构性差异（只有「发」没有「收」）：

* **纯出站**：没有入站轮询/长连接，``start()`` 无事可做，``running`` 恒为
  False。它是给用户自己的自动化（n8n / 企业微信群机器人 / 自建服务等）
  供料的单向口——Agent 回复、定时任务摘要、流水线节点行、每日运行日报
  都经 ``send_text`` 变成一条 POST。
* **allowed_ids 语义反转**：聊天渠道里「名单空 = 拒绝一切」是防陌生人套
  话的入站闸门；webhook 没有入站，自然不需要名单。``allowed_ids`` 在这里
  留空代表广播（configured + enabled 即推）；填了也只随配置原样存取，
  不参与任何判定（推送目标口径见 automation._cron_push_targets_of）。
* **签名**：配置了 ``secret`` 时，每次发送对原始请求体做 HMAC-SHA256，
  放在 ``X-SkySheep-Signature: sha256=<hex>`` 头里（GitHub webhook 同款
  形态）——接收方用同一 secret 重算比对，即可确认请求确实来自本机
  SkySheep、正文没有被篡改。

安全边界：URL 与 secret 是用户在本机设置里填的（channel.* 写操作在
dispatch 层仅限本机调用），不是模型可指定的参数；本适配器不读文件、
不执行命令，只把给用户的出站文本 POST 出去。web_fetch 的 SSRF 防护是
工具层的另一条线，这里不涉及也不放松。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlsplit

import httpx

from .base import Channel

logger = logging.getLogger("skysheep.channels.webhook")

# 默认请求超时（秒）：推送方都是 fire-and-forget 的后台任务，不该被慢端点拖住
DEFAULT_TIMEOUT_S = 10.0

# 签名头与 UA。签名值带算法前缀（sha256=…），接收方可按算法名分发校验。
SIGNATURE_HEADER = "X-SkySheep-Signature"
USER_AGENT = "SkySheep-Webhook/1.0"


def _host_of(url: str) -> str:
    """URL 的 <host:port> 部分（状态展示用）。

    不回显完整地址：query 里常带 token，落到日志/状态接口等于泄露。
    """
    try:
        parts = urlsplit(url.strip())
        return parts.netloc or (parts.path.split("/")[0] if parts.path else "")
    except ValueError:
        return ""


class WebhookChannel(Channel):
    name = "webhook"
    pure_outbound = True

    def __init__(self, config: dict, on_message) -> None:
        super().__init__(config, on_message)
        self.url = str(self.config.get("url", "") or "").strip()
        self.secret = str(self.config.get("secret", "") or "").strip()
        try:
            self.timeout_s = float(self.config.get("timeout_s") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            self.timeout_s = DEFAULT_TIMEOUT_S
        self._client: httpx.AsyncClient | None = None
        self._transport: httpx.AsyncBaseTransport | None = None  # 测试注入点
        # 在途推送计数：stop() 要等它们落地再关连接（见 stop 注释）。
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()  # 初始没有在途请求

    def configured(self) -> bool:
        return bool(self.url)

    # ---- 生命周期 ----

    async def start(self) -> None:
        """纯出站：没有后台任务可拉起。enabled 即「可用」，不占常驻资源。"""
        return None

    async def stop(self) -> None:
        await super().stop()
        # 关连接前先等在途推送落地（有界）：直接 aclose 会把还在飞的请求从脚
        # 下抽掉，让它们以一条空原因的失败收场（连接池已关时 httpcore 抛的
        # 就是 str() 为空的异常）。上限取 timeout_s + 1：httpx 的连接/读写超
        # 时都按 timeout_s 逐段约束，正常在途最迟也就这么久；万一卡过上限，
        # 放弃等待照旧关闭，在途请求各自的失败路径仍会记下带类型的异常。
        if self._inflight:
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=self.timeout_s + 1.0)
            except TimeoutError:  # 3.11+ 与 asyncio.TimeoutError 同一异常
                logger.warning(
                    "webhook 渠道关闭：在途推送 %d 个超时未归，照旧关闭", self._inflight
                )
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=min(5.0, self.timeout_s)),
                transport=self._transport,
                # 不跟随重定向：目标写错时把 3xx 原样报成失败，而不是换个地址继续 POST
                follow_redirects=False,
            )
        return self._client

    def _payload(self, chat_id: str, text: str) -> bytes:
        """出站 JSON 的固定形状。chat_id 对 webhook 无意义，原样透传留作备注。"""
        body = {
            "channel": self.name,
            "chat_id": str(chat_id or ""),
            "text": str(text or ""),
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _sign(secret: str, body: bytes) -> str:
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    # ---- 出站 ----

    async def send_text(self, chat_id: str, text: str) -> bool:
        """把文本 POST 成 JSON。失败不抛（调用方要能继续）：False + self.error。

        无平台限长，不做切分——接收方是程序，切分反而破坏结构化消费。
        """
        if not self.configured():
            self.error = "还没有配置推送地址 URL"
            return False
        body = self._payload(chat_id, text)
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.secret:
            headers[SIGNATURE_HEADER] = self._sign(self.secret, body)
        self._inflight += 1
        if self._inflight == 1:
            self._idle.clear()
        try:
            try:
                resp = await self._http().post(self.url, content=body, headers=headers)
            except Exception as e:  # noqa: BLE001 - 不可达/超时/坏 URL 都算发送失败
                # str(e) 可能为空（TimeoutError()、连接池已关的空消息异常），
                # 空时退回 repr：原因里始终带异常类型，不让日志只剩「失败：」。
                reason = str(e).strip() or repr(e)
                self.error = f"推送失败：{reason}"
                logger.warning("webhook 推送 %s 失败：%s", _host_of(self.url), reason)
                return False
            if not (200 <= resp.status_code < 300):
                self.error = f"推送未确认：HTTP {resp.status_code}"
                logger.warning("webhook 推送 %s 失败：%s", _host_of(self.url), self.error)
                return False
            self.error = ""
            return True
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    def status(self):
        st = super().status()
        st.extra = {
            "url_host": _host_of(self.url),
            "has_secret": bool(self.secret),
        }
        return st
