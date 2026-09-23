"""微信渠道：腾讯 iLink Bot API（官方「微信 ClawBot」能力）。

与飞书适配器的**结构性差异**（不是细节差异，是设计上的三处不同）：

1. **登录方式**：微信不给静态 Token。必须调 `get_bot_qrcode` 拿二维码，用户在手机微信里
   扫码确认后，轮询 `get_qrcode_status` 换取 `bot_token`。所以这个渠道必须有一个交互式
   登录流程（桌面端展示二维码 → 轮询 → 存 token），不能在配置里填一个字符串就完事。
2. **回复必须带 context_token**：每条入站消息带一个 `context_token`，回复时要原样带上，
   否则消息关联不到正确的对话窗口。因此本适配器要记住「chat_id → 最新 context_token」。
3. **游标而非 offset**：收消息用 `get_updates_buf` 游标（类似不透明的续读指针），
   必须每次用响应里的新游标覆盖旧值，否则会重复收到消息。游标要持久化。

协议来源：腾讯官方 npm 包 `@tencent-weixin/openclaw-weixin`（author: Tencent，MIT），
已按源码核对请求头、长轮询形状与 sendmessage 结构。官方文档：
https://developers.weixin.qq.com/doc/aispeech/knowledge/openapi/Clawbotrelated.html

安全：与飞书渠道同一姿态——allowed_ids 为空即拒绝一切；未授权来源只记录不回复。
另外注意 `-14`（session timeout / token 失效）：官方实现的处理是**冷却一小时**再重试，
本适配器沿用这个策略，并把状态告诉宿主人以便界面提示「需要重新登录」。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time

import httpx

from .base import Channel, ChannelMessage, split_text

logger = logging.getLogger("skysheep.channels.weixin")

# 固定接入域名（官方文档与 npm 包都写死这个值）
API_BASE = "https://ilinkai.weixin.qq.com"

# 官方 CLI 安装包对应的 bot_type；源码硬编码 3，含义未公开
BOT_TYPE = "3"

# iLink-App-Id：官方包从 package.json 的 ilink_appid 读取，值为 "bot"
ILINK_APP_ID = "bot"

# channel_version 上报值：官方包用自身版本号，这里报一个稳定的通道版本
CHANNEL_VERSION = "1.0.2"

# 长轮询：服务器 hold 最长 35 秒
POLL_TIMEOUT = 35.0

# 单条消息长度上限。官方包未公开确切值，取一个保守值并在超长时切分。
MAX_TEXT = 1800

# token 失效后的冷却时长（官方 session-guard 是 1 小时）
STALE_TOKEN_ERRCODE = -14
STALE_COOLDOWN = 3600.0

# 扫码登录的轮询间隔；服务器侧是长轮询（hold 最多 35 秒）
QR_POLL_TIMEOUT = 35.0


class WeixinChannel(Channel):
    name = "weixin"

    def __init__(self, config: dict, on_message) -> None:
        super().__init__(config, on_message)
        self._client: httpx.AsyncClient | None = None
        self._transport: httpx.AsyncBaseTransport | None = None  # 测试注入点
        # 登录态：优先取配置里已保存的（宿主持久化），没有则需扫码
        self.bot_token = str(self.config.get("bot_token", "") or "").strip()
        self.base_url = str(self.config.get("base_url", "") or API_BASE).rstrip("/")
        # 长轮询游标：必须持久化，否则重启后会重复收到旧消息
        self.cursor = str(self.config.get("cursor", "") or "")
        # chat_id → 最近一条入站消息的 context_token（回复时必须回传）
        self._contexts: dict[str, str] = {}
        # token 失效冷却截止时间戳（0 = 未冷却）
        self._paused_until = 0.0
        self.need_relogin = False

    def configured(self) -> bool:
        return bool(self.bot_token)

    # ---- HTTP 基础 ----

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(POLL_TIMEOUT + 15.0, connect=10.0),
                transport=self._transport,
            )
        return self._client

    @staticmethod
    def _headers(token: str = "", with_auth: bool = True) -> dict:
        """构造请求头。

        X-WECHAT-UIN 是防重放设计：每次请求随机一个 uint32 → 十进制字符串 → base64。
        官方包每个请求都重新生成，这里保持一致。
        """
        headers = {
            "Content-Type": "application/json",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(_client_version()),
        }
        if with_auth:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["X-WECHAT-UIN"] = base64.b64encode(
                str(random.randint(0, 0xFFFFFFFF)).encode("utf-8")
            ).decode("ascii")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _base_info(self) -> dict:
        return {"channel_version": CHANNEL_VERSION, "bot_agent": "SkySheep/1.0"}

    async def _get(self, endpoint: str, *, token: str = "", timeout: float | None = None) -> dict | None:
        try:
            resp = await self._http().get(
                f"{self.base_url}/{endpoint}",
                headers=self._headers(token),
                timeout=timeout or httpx.USE_CLIENT_DEFAULT,
            )
        except Exception as e:  # noqa: BLE001 - 网络抖动不能让渠道挂掉
            self.error = f"{endpoint} 网络异常：{e}"
            logger.warning("weixin %s 失败：%s", endpoint, e)
            return None
        return self._parse(resp, endpoint)

    async def _post(self, endpoint: str, payload: dict, *, token: str = "",
                    timeout: float | None = None) -> dict | None:
        try:
            resp = await self._http().post(
                f"{self.base_url}/{endpoint}",
                headers=self._headers(token),
                json=payload,
                timeout=timeout or httpx.USE_CLIENT_DEFAULT,
            )
        except Exception as e:  # noqa: BLE001 - 长轮询超时属正常控制流
            self.error = f"{endpoint} 网络异常：{e}"
            logger.debug("weixin %s 异常：%s", endpoint, e)
            return None
        return self._parse(resp, endpoint)

    def _parse(self, resp: httpx.Response, endpoint: str) -> dict | None:
        if resp.status_code != 200:
            self.error = f"{endpoint} HTTP {resp.status_code}：{resp.text[:200]}"
            logger.warning("weixin %s HTTP %s", endpoint, resp.status_code)
            return None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            self.error = f"{endpoint} 返回不是 JSON"
            return None
        # ret/errcode 非 0 都算失败；-14 是 token 失效，单独标记
        code = data.get("ret", data.get("errcode", 0))
        if code not in (0, None):
            if code == STALE_TOKEN_ERRCODE:
                self._pause_for_stale_token()
                return data
            self.error = f"{endpoint}：{data.get('errmsg') or data.get('err_msg') or code}"
            return data
        self.error = ""
        return data

    def _pause_for_stale_token(self) -> None:
        """token 失效：进入冷却并标记需重新登录（对齐官方 session-guard）。"""
        self._paused_until = time.time() + STALE_COOLDOWN
        self.need_relogin = True
        self.error = "登录已失效，需要重新扫码登录"
        logger.warning("weixin token 失效（-14），冷却 %s 秒", int(STALE_COOLDOWN))

    @property
    def paused(self) -> bool:
        return time.time() < self._paused_until

    # ---- 扫码登录（交互式，供桌面端调用） ----

    async def fetch_qrcode(self) -> dict:
        """取登录二维码。返回 {qrcode, url}；失败抛 RuntimeError 带可读原因。

        注意：官方包是 POST 且带 local_token_list（便于服务器识别已登录过的账号）。
        首次登录本地没有 token，传空列表即可。
        """
        data = await self._post(
            f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
            {"local_token_list": _known_tokens(self.config)},
            timeout=20.0,
        )
        if not data:
            raise RuntimeError(self.error or "获取二维码失败")
        qrcode = str(data.get("qrcode", "") or "")
        url = str(data.get("qrcode_img_content", "") or "")
        if not qrcode:
            raise RuntimeError(f"返回里没有 qrcode：{str(data)[:200]}")
        return {"qrcode": qrcode, "url": url}

    async def poll_qrcode(self, qrcode: str) -> dict:
        """轮询扫码状态。返回 {status, bot_token?, base_url?}。

        status: wait（等待扫描）/ scaned（已扫描）/ confirmed（已确认）/ expired（已过期）
        """
        resp = await self._get(
            f"ilink/bot/get_qrcode_status?qrcode={qrcode}",
            timeout=QR_POLL_TIMEOUT,
        )
        if resp is None:
            # 长轮询客户端超时属正常，按「继续等」处理
            return {"status": "wait"}
        code = resp.get("ret", resp.get("errcode", 0))
        if code not in (0, None):
            return {"status": "error", "error": str(resp.get("errmsg") or code)}
        status = str(resp.get("status", "") or "wait")
        result = {"status": status}
        if status == "confirmed":
            creds = resp.get("credentials") or {}
            token = str(creds.get("bot_token", "") or "")
            base = str(resp.get("baseurl", "") or resp.get("base_url", "") or "")
            if token:
                result["bot_token"] = token
            if base:
                result["base_url"] = base.rstrip("/")
        return result

    async def apply_login(self, bot_token: str, base_url: str = "") -> None:
        """把扫码得到的凭据落到适配器上（宿主负责持久化）。"""
        self.bot_token = bot_token.strip()
        if base_url:
            self.base_url = base_url.rstrip("/")
        self._paused_until = 0.0
        self.need_relogin = False
        self.error = ""
        await self._persist_state()

    async def _persist_state(self) -> None:
        """把运行时状态交给宿主持久化（token 与游标都会变）。"""
        if self.on_state is None:
            return
        try:
            await self.on_state({
                "bot_token": self.bot_token,
                "base_url": self.base_url,
                "cursor": self.cursor,
            })
        except Exception as e:  # noqa: BLE001 - 持久化失败不影响本轮
            logger.warning("weixin 状态持久化失败：%s", e)

    # ---- 生命周期 ----

    async def start(self) -> None:
        if not self.configured():
            self.error = "尚未扫码登录（请先在设置里扫描二维码）"
            self.need_relogin = True
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        await super().stop()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _poll_loop(self) -> None:
        while True:
            try:
                if self.paused:
                    # token 失效冷却：等一会儿再试，期间不刷日志
                    await asyncio.sleep(min(60.0, max(1.0, self._paused_until - time.time())))
                    continue
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 单轮失败不终止循环
                self.error = str(e)
                await asyncio.sleep(3)

    async def _poll_once(self) -> None:
        data = await self._post(
            "ilink/bot/getupdates",
            {"get_updates_buf": self.cursor, "base_info": self._base_info()},
            token=self.bot_token,
            timeout=POLL_TIMEOUT + 10.0,
        )
        if not data:
            await asyncio.sleep(1)
            return
        code = data.get("ret", data.get("errcode", 0))
        if code not in (0, None):
            # -14 已在 _parse 里进入冷却；其它错误退避一下
            await asyncio.sleep(2)
            return

        # 游标必须推进：否则会重复收到同一批消息
        new_cursor = data.get("get_updates_buf")
        advanced = bool(new_cursor) and new_cursor != self.cursor
        if advanced:
            self.cursor = str(new_cursor)
            await self._persist_state()

        for msg in data.get("msgs") or []:
            await self._handle_message(msg)

        # 保证每轮有确定的让出点：服务器不 hold 时避免紧密空转饿死事件循环
        if not advanced:
            await asyncio.sleep(0.5)

    async def _handle_message(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        # message_type: 1 = 用户发来，2 = 机器人发出；只处理用户消息
        if int(msg.get("message_type", 0) or 0) != 1:
            return
        from_user = str(msg.get("from_user_id", "") or "")
        text = _extract_text(msg)
        context_token = str(msg.get("context_token", "") or "")
        if not from_user:
            return
        # 记住 context_token：回复时必须原样回传，否则消息落不到正确窗口
        if context_token:
            self._contexts[from_user] = context_token
        if not text:
            return  # 图片/语音/文件：当前版本只处理文本
        await self.on_message(
            ChannelMessage(
                channel=self.name,
                actor=from_user,
                chat_id=from_user,
                text=text,
                approved=self.is_allowed(from_user, from_user),
                raw=msg,
            )
        )

    # ---- 出站 ----

    async def send_text(self, chat_id: str, text: str) -> bool:
        if not chat_id:
            return False
        if self.paused:
            return False
        token = self.bot_token
        if not token:
            self.error = "尚未扫码登录"
            return False
        context_token = self._contexts.get(chat_id, "")
        ok = True
        for chunk in split_text(text or "", MAX_TEXT):
            payload = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": _client_id(),
                    "message_type": 2,      # BOT
                    "message_state": 2,     # FINISH
                    "item_list": [{"type": 1, "text_item": {"text": chunk}}],
                    "base_info": self._base_info(),
                }
            }
            if context_token:
                payload["msg"]["context_token"] = context_token
            data = await self._post("ilink/bot/sendmessage", payload, token=token)
            if not data:
                ok = False
                continue
            code = data.get("ret", data.get("errcode", 0))
            if code not in (0, None):
                ok = False
        return ok

    def status(self):
        st = super().status()
        st.extra = {
            "need_relogin": self.need_relogin,
            "paused": self.paused,
            "logged_in": bool(self.bot_token),
        }
        return st


def _client_version() -> int:
    """iLink-App-ClientVersion：0x00MMNNPP。这里报 1.0.2 → 65666。"""
    return (1 << 16) | (0 << 8) | 2


def _client_id() -> str:
    """出站消息的 client_id：官方格式 {prefix}:{timestamp}-{8 位 hex}。"""
    return f"skysheep:{int(time.time() * 1000)}-{random.getrandbits(32):08x}"


def _extract_text(msg: dict) -> str:
    """从 item_list 里取第一条文本。微信消息是复合结构，文本只是其中一种 item。"""
    for item in msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        if int(item.get("type", 0) or 0) == 1:
            text_item = item.get("text_item") or {}
            text = text_item.get("text")
            if text is not None:
                return str(text)
    return ""


def _known_tokens(config: dict) -> list[str]:
    """已知 bot_token 列表（官方包用 local_token_list 让服务器识别老账号）。"""
    token = str(config.get("bot_token", "") or "").strip()
    return [token] if token else []


def parse_login_state(raw: str) -> dict:
    """解析宿主持久化的登录状态 JSON；坏数据返回空 dict（不让它炸启动）。"""
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
