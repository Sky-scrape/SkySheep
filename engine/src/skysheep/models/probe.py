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


# openai 兼容服务 /models 条目里可能出现上下文窗口的字段名（各厂家不统一，逐个试）。
# 注意 max_tokens 是输出上限不是窗口，不能混进来。
_CONTEXT_FIELDS = (
    "context_length",       # OpenRouter
    "context_window",
    "max_context_length",   # SiliconFlow
    "max_model_len",        # vLLM
    "context_size",
)


def _extract_context_limit(entry: dict) -> int | None:
    """从 /models 的单个模型条目里找上下文窗口字段；找到且像样（>0）才返回。"""
    for key in _CONTEXT_FIELDS:
        v = entry.get(key)
        if isinstance(v, int) and 0 < v < 100_000_000:
            return v
    # OpenRouter 把它嵌在 top_provider 子对象里
    tp = entry.get("top_provider")
    if isinstance(tp, dict):
        v = tp.get("context_length")
        if isinstance(v, int) and 0 < v < 100_000_000:
            return v
    return None


async def probe_context_limit(
    *,
    kind: str = "openai",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str = "",
    timeout_s: float = 15.0,
) -> dict:
    """探测某服务模型的最大上下文窗口。

    返回 {"limit": int|None, "note": str}：limit 为 None 表示这条路探测不到
    （接口不提供 / 列表里没有该模型），note 说明原因与建议。网络与鉴权错误
    直接抛 RuntimeError（复用 _friendly_error 的可读文案）。
    """
    base = (base_url or "").strip()
    model = (model or "").strip()

    if kind == "anthropic":
        # Anthropic 的 models 接口只回 id/名字/时间，不带窗口字段
        return {
            "limit": None,
            "note": "Anthropic 接口不提供窗口信息，请按官方文档填写（多数 200K；Sonnet 4 及以上支持 100 万）",
        }

    if kind == "ollama":
        url = (base.rstrip("/") if base else OLLAMA_BASE) + "/api/show"
        kw = _client_kwargs(base or OLLAMA_BASE, timeout_s)
        async with httpx.AsyncClient(timeout=timeout_s, **kw) as client:
            try:
                resp = await client.post(url, json={"model": model})
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise RuntimeError(_friendly_error(e, base or OLLAMA_BASE)) from e
        top = data.get("context_length")
        if isinstance(top, int) and top > 0:
            note = f"Ollama 报告 {model or '该模型'} 的上下文窗口 {top:,} tokens"
            return {"limit": top, "note": note}
        info = data.get("model_info") or {}
        vals = [
            v for k, v in info.items()
            if k.endswith(".context_length") and isinstance(v, int) and v > 0
        ]
        if vals:
            note = f"Ollama 报告 {model or '该模型'} 的上下文窗口 {max(vals):,} tokens"
            return {"limit": max(vals), "note": note}
        return {
            "limit": None,
            "note": "Ollama 没有返回该模型的窗口信息（模型可能还没 pull 下来，或版本太旧）",
        }

    # openai 兼容：GET {base}/models，在模型条目里找窗口字段
    url = (base or "https://api.openai.com/v1").rstrip("/") + "/models"
    headers = {"Authorization": "Bearer " + (api_key or "")} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout_s, **_client_kwargs(base, timeout_s)) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise RuntimeError(_friendly_error(e, base or None)) from e

    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {"limit": None, "note": "该服务的模型列表不是标准格式，无法自动检测；请按官方文档填写"}
    target = None
    if model:
        for it in entries:
            if isinstance(it, dict) and it.get("id") == model:
                target = it
                break
        if target is None:
            return {
                "limit": None,
                "note": f"模型列表里没有「{model}」；先点「检测并获取模型」确认名称，或按官方文档填写",
            }
    elif len(entries) == 1 and isinstance(entries[0], dict):
        target = entries[0]
    if target is None:
        return {"limit": None, "note": "请先在「模型」里填上要检测的模型 ID（或先启用一个模型）"}

    limit = _extract_context_limit(target)
    if limit:
        return {"limit": limit, "note": f"检测到 {target.get('id', model)} 的上下文窗口 {limit:,} tokens"}
    return {"limit": None, "note": "该服务的模型列表不带窗口字段，无法自动检测；请按官方文档填写"}
