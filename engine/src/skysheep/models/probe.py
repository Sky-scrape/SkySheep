"""本地模型探测：Ollama / LM Studio 等本地推理服务的可用模型列表。"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

OLLAMA_BASE = "http://localhost:11434"
LMSTUDIO_BASE = "http://localhost:1234/v1"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}


def _is_local(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".local") or host.endswith(".localhost")


def _client_kwargs(base_url: str | None, timeout_s: float) -> dict:
    """本地地址不走系统代理。

    httpx 默认 trust_env=True 会读取系统代理（Windows 上来自注册表），把
    127.0.0.1:11434 这类请求也转给代理，轻则变慢、重则直接 502——本地服务
    必须直连。
    """
    if not _is_local(base_url):
        return {}
    return {"http_client": httpx.AsyncClient(trust_env=False, timeout=timeout_s)}


async def probe_ollama(base_url: str = OLLAMA_BASE, timeout_s: float = 2.0) -> list[str]:
    """返回本机 Ollama 已拉取的模型名列表；服务未运行时返回空列表。"""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(base_url.rstrip("/") + "/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


async def probe_provider_models(
    *,
    kind: str = "openai",
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_s: float = 15.0,
) -> list[str]:
    """向模型服务查询它当前可用的模型名列表。

    走各家 SDK 自己的 models.list()（而不是手拼 HTTP 请求）：鉴权头、base_url
    拼接、Anthropic 的版本头都由 SDK 负责，行为与真正的对话调用一致——能列出
    模型基本就说明 Key 和地址是对的。

    失败时抛 RuntimeError，消息直接给用户看（Key 无效 / 地址不对 / 连不上）。
    """
    if kind not in ("openai", "anthropic"):
        raise RuntimeError("未知的协议类型: " + str(kind))
    if not api_key:
        raise RuntimeError("需要先填写 API Key 才能检测可用模型")

    base = (base_url or "").strip() or None
    extra = _client_kwargs(base, timeout_s)
    try:
        if kind == "anthropic":
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(
                api_key=api_key, base_url=base, timeout=timeout_s, max_retries=0, **extra
            )
            page = await client.models.list(limit=1000)
        else:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=api_key, base_url=base, timeout=timeout_s, max_retries=0, **extra
            )
            page = await client.models.list()
    except Exception as e:  # 网络/鉴权/解析错误统一转成可读提示
        raise RuntimeError(_friendly_error(e, base)) from e

    ids = {str(getattr(m, "id", "") or "").strip() for m in getattr(page, "data", []) or []}
    return sorted(i for i in ids if i)


def _friendly_error(e: Exception, base: str | None) -> str:
    """把 SDK 抛出的异常翻译成用户能照着改的提示。"""
    status = getattr(e, "status_code", None)
    where = base or "默认接口地址"
    if status in (401, 403):
        return f"API Key 无效或没有权限（{where}）"
    if status == 404:
        return f"接口地址不对，或该服务不提供模型列表（{where}）"
    if status in (502, 503, 504):
        return f"服务无响应（{status}）（{where}）：确认地址正确、服务已启动；若本机开了代理，本地地址需直连"
    if status is not None:
        return f"服务返回错误 {status}（{where}）"
    text = str(e)
    low = text.lower()
    if "connect" in low or "timeout" in low or "timed out" in low:
        return f"连不上 {where}，请检查地址、网络或代理"
    return "检测失败：" + (text.splitlines()[0] if text else e.__class__.__name__)
