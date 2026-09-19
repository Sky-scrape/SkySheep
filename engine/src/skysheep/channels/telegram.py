"""Telegram 渠道：官方 Bot API 长轮询。

为什么不用 python-telegram-bot：本项目依赖已经很重（打包体积、启动时间），
而这里需要的能力只有两个接口——getUpdates 收、sendMessage 发。用现有的 httpx
直接调 REST 即可，省掉一个框架依赖和它的版本演进。

安全前提：Telegram 平台侧由 bot token 认证，但**任何知道 bot 用户名的人都能给它
发消息**。所以 allowed_ids 是刚性要求，不是可选优化——空名单等于拒绝一切。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .base import Channel, ChannelMessage, split_text

logger = logging.getLogger("skysheep.channels.telegram")

API_BASE = "https://api.telegram.org"

# Telegram 单条消息上限 4096 字符；留出余量给前缀与省略号
MAX_TEXT = 3900

# 长轮询等待秒数：由服务器 hold 住连接，直到有消息或超时。
# 25 秒是社区常用值——比 Telegram 上限（50）短，留出网络抖动余量。
POLL_TIMEOUT = 25


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, config: dict, on_message) -> None:
        super().__init__(config, on_message)
        self._offset = 0
        self._client: httpx.AsyncClient | None = None
        # 测试注入点：传 None 时用真实网络
        self._transport: httpx.AsyncBaseTransport | None = None

    def configured(self) -> bool:
        return bool(str(self.config.get("token", "")).strip())

    # ---- HTTP 基础 ----

    def _api(self, method: str) -> str:
        return f"{API_BASE}/bot{str(self.config.get('token', '')).strip()}/{method}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(POLL_TIMEOUT + 10.0, connect=10.0),
                transport=self._transport,
            )
        return self._client

    async def _call(self, method: str, payload: dict) -> dict | None:
        """调一个 Bot API 方法。失败返回 None 并记 error，不抛——轮询循环要能继续。"""
        try:
            resp = await self._http().post(self._api(method), json=payload)
        except Exception as e:  # noqa: BLE001 - 网络抖动不能让整个渠道挂掉
            self.error = f"{method} 网络异常：{e}"
            logger.warning("telegram %s 失败：%s", method, e)
            return None
        if resp.status_code != 200:
            # 401 = token 错；409 = 同 token 有另一个 getUpdates 在跑。两者都要让用户看见。
            detail = resp.text[:200]
            self.error = f"{method} HTTP {resp.status_code}：{detail}"
            logger.warning("telegram %s HTTP %s：%s", method, resp.status_code, detail)
            return None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            self.error = f"{method} 返回不是 JSON"
            return None
        if not data.get("ok"):
            self.error = f"{method}：{data.get('description', '未知错误')}"
            return None
        self.error = ""
        return data

    # ---- 生命周期 ----

    async def start(self) -> None:
        if not self.configured():
            self.error = "未填写 Bot Token，无法启动"
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
        # 启动前先丢掉历史积压：否则重启后会把停机期间的消息全刷一遍
        await self._drain_backlog()
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 单轮失败不终止循环
                self.error = str(e)
                await asyncio.sleep(3)

    async def _drain_backlog(self) -> None:
        """把 offset 推到最新，跳过停机期间的消息（避免重启后刷屏）。"""
        data = await self._call("getUpdates", {"offset": -1, "timeout": 0})
        if not data:
            return
        for upd in data.get("result") or []:
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)

    async def _poll_once(self) -> None:
        data = await self._call(
            "getUpdates",
            {"offset": self._offset, "timeout": POLL_TIMEOUT, "allowed_updates": ["message"]},
        )
        if not data:
            await asyncio.sleep(2)
            return
        advanced = False
        for upd in data.get("result") or []:
            uid = int(upd.get("update_id", 0)) + 1
            if uid > self._offset:
                self._offset = uid
                advanced = True
            await self._handle_update(upd)

        # 保证本循环每轮都有一个确定的让出点。
        #
        # 长轮询正常时由服务器 hold 住（POLL_TIMEOUT 秒），await 自然让出；
        # 但若服务器/中间代理不 hold 而是立刻返回空（或把已处理过的消息重放回来），
        # 这个循环就没有任何真正会让出控制权的 await —— HTTP 层同步返回时
        # await 不切换任务，于是变成 CPU 忙等，把整个事件循环饿死。
        # 空结果与重放各退避一次，代价可忽略（下一轮顶多晚半秒）。
        if not data.get("result"):
            await asyncio.sleep(0.5)
        elif not advanced:
            await asyncio.sleep(1)

    async def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        chat_id = str(chat.get("id", ""))
        actor = str(sender.get("id", "") or chat_id)
        text = str(msg.get("text", "") or "")
        if not text or not chat_id:
            return  # 图片/贴纸/加入群等无文本事件：当前版本不处理
        await self.on_message(
            ChannelMessage(
                channel=self.name,
                actor=actor,
                chat_id=chat_id,
                text=text,
                approved=self.is_allowed(actor, chat_id),
                raw=msg,
            )
        )

    # ---- 出站 ----

    async def send_text(self, chat_id: str, text: str) -> bool:
        if not str(chat_id).strip():
            return False
        ok = True
        for chunk in split_text(text or "", MAX_TEXT):
            data = await self._call(
                "sendMessage",
                # 关掉链接预览：Agent 回复里常有 URL，展开会挤满手机屏幕
                {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            )
            ok = ok and bool(data)
        return ok


_split = split_text  # 向后兼容：旧调用点与测试用这个私有名
