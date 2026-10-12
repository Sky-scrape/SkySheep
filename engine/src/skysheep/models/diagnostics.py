"""模型层诊断：错误分类 + 中文建议 + 连通性测试 + 供应商健康状态。

独立小模块（不依赖 Provider 类，纯函数 + 轻量数据类），供三处消费：
- server 层「诊断」RPC（model.test / model.diagnostics / model.providers_health）
- agent 主循环：ErrorEvent 附带分类与建议（诊断卡）
- 前端设置页：测试连接按钮、供应商状态灯、错误文案

测试连接用最小真实调用（非流式、max_tokens=1）验证：Key 有效性、
base_url 可达、模型名可用。只测一次，不做重试（诊断要快、要如实）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ---- 错误分类 ----

# (类别, 状态码/特征串)。匹配顺序即优先级：状态码优先于文本特征。
# 状态码取自异常的 status_code / response.status_code（OpenAI/Anthropic SDK、
# httpx 都挂在这两处之一）。

ERROR_CATEGORIES: tuple[tuple[str, tuple[int, ...], tuple[str, ...]], ...] = (
    (
        "auth",
        (401, 403),
        (
            "invalid api key", "invalid_api_key", "unauthorized", "authentication",
            "invalid x-api-key", "permission denied", "forbidden", "access denied",
            "无效的 api key", "鉴权失败", "认证失败",
        ),
    ),
    (
        "rate_limit",
        (429,),
        (
            "rate limit", "rate_limit", "too many requests", "限流",
            "请求过于频繁", "quota exceeded",
        ),
    ),
    (
        "balance",
        (),
        (
            "insufficient", "insufficient balance", "余额不足", "arrears",
            "exceeded your current quota", "billing", "欠费", "no enough balance",
            "account is disabled", "账户已停用",
        ),
    ),
    (
        "context_overflow",
        (),
        (
            "context length", "context_length", "maximum context", "max context",
            "too many tokens", "reduce the length", "prompt is too long",
            "exceed context", "上下文长度", "上下文超", "超过最大长度", "token 数量超过",
        ),
    ),
    (
        "model_missing",
        (404,),
        (
            "model not found", "model_not_found", "does not exist", "no such model",
            "unknown model", "model does not exist", "模型不存在", "无效的模型",
        ),
    ),
    (
        "network",
        (),
        (
            "timeout", "timed out", "connect", "connection reset", "connection aborted",
            "connection refused", "connection closed", "getaddrinfo failed",
            "name or service not known", "network", "ssl", "eof occurred",
            "代理", "proxy", "无法连接", "连接超时",
        ),
    ),
    (
        "server",
        (500, 502, 503, 504, 529),
        (
            "server error", "bad gateway", "service unavailable", "overloaded",
            "temporarily unavailable", "internal server error", "服务器",
        ),
    ),
    (
        "invalid_request",
        (400, 422),
        (
            "invalid request", "bad request", "invalid parameter", "invalid_parameter",
            "unsupported", "not supported", "max_tokens", "temperature",
            "无效的请求", "参数",
        ),
    ),
)

# 每类的中文标题与可操作建议（用户照着做就能解决的写法；不猜具体原因）
ADVICE: dict[str, tuple[str, str]] = {
    "auth": (
        "API Key 无效或没有权限",
        "打开 ⚙ 设置 · 模型服务，重新粘贴正确的 API Key；"
        "确认 Key 未被删除或禁用、所属账号有该模型的访问权限。"
        "若用环境变量，检查变量名与值是否正确。",
    ),
    "rate_limit": (
        "触发限流（请求过于频繁或配额用尽）",
        "稍等片刻再试（应用会自动重试几次）；持续出现时在服务商控制台"
        "查看速率与配额档位，或更换其他已配置的模型服务。"
        "这类失败请求一般不计费（以账单为准）；也可在 设置 · 高级 里配置"
        "「备用模型」，故障时自动接管。",
    ),
    "balance": (
        "账户余额不足或配额已用完",
        "登录服务商控制台充值或购买配额；也可以切换到其他已配置的模型服务。",
    ),
    "context_overflow": (
        "对话长度超过模型上下文窗口",
        "在输入框执行 /compact 压缩历史；或在 设置 · 模型服务 里核对该服务的"
        "「上下文上限」是否与模型真实窗口一致。",
    ),
    "model_missing": (
        "模型名不存在或不可用",
        "在 设置 · 模型服务 里点「检测并获取模型」确认模型 ID 的准确拼写；"
        "确认当前账号有该模型的访问权限（部分模型需要申请）。"
        "也可先切换到其他可用模型服务继续工作。",
    ),
    "network": (
        "网络连接失败",
        "检查网络与代理设置：该服务走代理时确认代理已启动且端口正确；"
        "本地服务（Ollama 等）需要直连，若开了代理请把 localhost 加入直连名单。",
    ),
    "server": (
        "模型服务暂时不可用",
        "上游服务正在异常或过载，通常稍等即恢复；持续出现时到服务商状态页确认，"
        "或先切换到其他已配置的模型服务。"
        "这类失败请求一般不计费（以账单为准）；也可在 设置 · 高级 里配置"
        "「备用模型」，故障时自动接管。",
    ),
    "invalid_request": (
        "请求参数不被该服务接受",
        "多为模型与参数不兼容（如思考强度、温度范围）：在 设置 · 模型服务 里"
        "关掉该服务用不到的开关（如「思考强度」），或换个模型再试。",
    ),
    "unknown": (
        "调用出错",
        "具体原因见上方错误信息；重试一次仍失败时，可在 设置 · 关于 导出诊断包反馈。",
    ),
}


@dataclass
class ErrorDiagnosis:
    """一次模型错误的诊断结果：category + 中文标题 + 建议。"""

    category: str
    title: str
    advice: str
    status_code: int | None = None
    transient: bool = False


def _status_of(e: Exception) -> int | None:
    status = getattr(e, "status_code", None)
    if status is None:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


def diagnose_error(e: Exception) -> ErrorDiagnosis:
    """把上游异常分类并给出中文建议。永不抛异常。"""
    from ..core.provider_errors import is_transient_error

    status = _status_of(e)
    text = str(e).lower()
    category = "unknown"
    for cat, codes, markers in ERROR_CATEGORIES:
        if status is not None and status in codes:
            category = cat
            break
        if any(m in text for m in markers):
            category = cat
            break
    # 404 且文本没命中的兜底：测试连接最常见的"地址不对"
    if category == "unknown" and status == 404:
        category = "model_missing"
    title, advice = ADVICE.get(category, ADVICE["unknown"])
    return ErrorDiagnosis(
        category=category,
        title=title,
        advice=advice,
        status_code=status,
        transient=is_transient_error(e),
    )


# ---- 连通性测试 ----


@dataclass
class TestResult:
    """model.test 的结果：ok + 各探测项 + 耗时 + 失败时的诊断。"""

    ok: bool
    latency_ms: int = 0
    model_listed: bool | None = None  # /models 列表里能找到该模型（None = 无法判定）
    context_limit: int | None = None  # /models 报告的窗口（拿不到为 None）
    model: str = ""
    error: str = ""
    category: str = ""
    title: str = ""
    advice: str = ""
    tested_at: float = field(default_factory=time.time)


def _friendly_test_error(e: Exception, base: str | None) -> str:
    """测试连接的失败文案：复用 probe 的翻译思路，指名地址。"""
    from .probe import _friendly_error  # 局部导入避免循环

    return _friendly_error(e, base)


async def test_connection(
    *,
    kind: str = "openai",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str = "",
    timeout_s: float = 20.0,
    transport: object | None = None,
) -> TestResult:
    """对单个模型服务做一次最小真实调用，返回连通性与能力探测结果。

    流程：models.list 找模型拿窗口字段 → 用 1 token 的非流式请求实测对话。
    models.list 失败不提前失败（部分服务不提供该接口）：仍会尝试对话调用。
    对话调用成功即判定连通；失败时给出分类诊断。transport 仅供测试注入
    （httpx.MockTransport），注入后整条链路不再打真实网络。
    """
    from .probe import _extract_context_limit, _is_local, http_client_module

    base = (base_url or "").strip()
    model = (model or "").strip()
    result = TestResult(ok=False, model=model)

    def _client_extra() -> dict:
        """每次新建客户端都单独建一个 httpx 客户端。

        不能共用一个 http_client：SDK 的 close() 会把它一并关掉，第一段
        （models.list）收尾后再给第二段（对话调用）同一个已关闭的连接池，
        本地服务会直接报 Connection error——本地测试连接必挂。
        transport 仅供测试注入（httpx.MockTransport），注入后整条链路不打真实网络；
        httpx 模块按协议取（anthropic 1.5+ 只认 httpx2）。
        """
        hx = http_client_module(kind)
        if transport is not None:
            return {"http_client": hx.AsyncClient(transport=transport, timeout=timeout_s)}
        if base and _is_local(base):
            # 本地服务直连（probe 同款：系统代理会把 127.0.0.1 转走）
            return {"http_client": hx.AsyncClient(trust_env=False, timeout=timeout_s)}
        return {}

    t0 = time.monotonic()

    # 1) 模型列表：确认 Key/地址可用 + 收集窗口字段（失败不提前失败）
    model_listed: bool | None = None
    ctx_limit: int | None = None
    try:
        if kind == "anthropic":
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(
                api_key=api_key or "", base_url=base or None,
                timeout=timeout_s, max_retries=0, **_client_extra(),
            )
        else:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=api_key or "", base_url=base or None,
                timeout=timeout_s, max_retries=0, **_client_extra(),
            )
        try:
            if kind == "anthropic":
                page = await client.models.list(limit=1000)
            else:
                page = await client.models.list()
            ids = {str(getattr(m, "id", "") or "") for m in getattr(page, "data", []) or []}
            if model:
                model_listed = model in ids
            # 在原始条目里找窗口字段：SDK 的 model 对象不一定带，重查一次原始 JSON
            try:
                hx = http_client_module(kind)

                url = (base or "https://api.openai.com/v1").rstrip("/") + "/models"
                headers = (
                    {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
                    if kind == "anthropic"
                    else {"Authorization": "Bearer " + (api_key or "")}
                )
                async with hx.AsyncClient(
                    timeout=timeout_s, trust_env=not (base and _is_local(base)),
                    transport=transport,
                ) as hc:
                    resp = await hc.get(url, headers=headers)
                    if resp.status_code == 200:
                        entries = resp.json().get("data") or []
                        for it in entries:
                            if isinstance(it, dict) and it.get("id") == model:
                                ctx_limit = _extract_context_limit(it)
                                break
            except Exception:  # noqa: BLE001 - 窗口探测失败不影响连通判定
                pass
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001 - list 失败转诊断，继续对话调用
        diag = diagnose_error(e)
        result.error = f"{_friendly_test_error(e, base)}（{diag.title}）"
        result.category = diag.category
        result.title = diag.title
        result.advice = diag.advice
        result.latency_ms = int((time.monotonic() - t0) * 1000)
        # 鉴权/网络类失败直接返回：对话调用注定同样失败
        if diag.category in ("auth", "network", "server"):
            return result

    # 2) 对话调用：1 token、非流式，验证模型真的可用
    try:
        if kind == "anthropic":
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(
                api_key=api_key or "", base_url=base or None,
                timeout=timeout_s, max_retries=0, **_client_extra(),
            )
            resp = await client.messages.create(
                model=model or "claude-3-haiku-20240307",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            _ = resp.content
        else:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=api_key or "", base_url=base or None,
                timeout=timeout_s, max_retries=0, **_client_extra(),
            )
            resp = await client.chat.completions.create(
                model=model or "gpt-4o-mini",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            _ = resp.choices
        result.ok = True
        result.latency_ms = int((time.monotonic() - t0) * 1000)
        result.model_listed = model_listed
        result.context_limit = ctx_limit
        return result
    except Exception as e:  # noqa: BLE001
        diag = diagnose_error(e)
        result.latency_ms = int((time.monotonic() - t0) * 1000)
        result.model_listed = model_listed
        result.context_limit = ctx_limit
        result.error = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
        result.category = diag.category
        result.title = diag.title
        result.advice = diag.advice
        return result


# ---- 能力探测（流式 / 工具调用 / 图片输入 / 思考强度） ----

# 「测试连接」只回答能不能对话；这里对四项能力各发一次最小真实调用，
# 回答「这个模型能干什么」。由用户点按钮触发（每次都会产生少量真实计费），
# 不自动跑。四项彼此独立：单项失败不影响其他项；结果三态——True 支持 /
# False 明确不支持（服务拒绝且报错点名该能力）/ None 没测成（鉴权、网络等
# 与能力无关的失败），None 不参与结论，只如实说明原因。

# 1×1 透明 PNG（最小合法图片）：视觉探测用它，几十字节、各家都按 1 个
# 图片 token 计，把探测成本压到最低
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z/D/PwAG/gL+DHWJ3gAAAABJRU5ErkJggg=="
)

# 服务拒绝请求时，报错文本里点名了哪项能力才算「明确不支持」；
# 没点名的 400 可能是别的原因（温度、其他参数），宁可不判
_FEATURE_MARKERS: dict[str, tuple[str, ...]] = {
    "streaming": ("stream",),
    "tools": ("tool", "function"),
    "vision": ("image", "vision", "media", "multimodal"),
    "reasoning": ("reason", "think", "effort", "budget"),
}

_CAP_LABELS: dict[str, str] = {
    "streaming": "流式输出",
    "tools": "工具调用",
    "vision": "图片输入",
    "reasoning": "思考强度",
}


@dataclass
class CapabilityItem:
    """一项能力的探测结果：supported 三态 + 给人看的说明。"""

    supported: bool | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {"supported": self.supported, "note": self.note}


def _cap_item_from_error(e: Exception, feature: str,
                         markers: tuple[str, ...]) -> CapabilityItem:
    """把探测调用的失败分类成三态结论。永不抛异常。"""
    text = str(e)
    low = text.lower()
    diag = diagnose_error(e)
    first = (text.splitlines()[0][:140] if text else type(e).__name__)
    if diag.category == "invalid_request" and any(m in low for m in markers):
        return CapabilityItem(False, f"该服务明确拒绝了{feature}（不支持）：{first}")
    if diag.category in ("auth", "network", "server", "rate_limit", "balance"):
        return CapabilityItem(None, f"没测成：{diag.title}（{first}）")
    if diag.category == "invalid_request":
        return CapabilityItem(None, f"服务拒绝了请求但报错未点名{feature}，无法判定：{first}")
    return CapabilityItem(None, f"没测成：{first}")


def capability_summary(items: dict[str, CapabilityItem]) -> str:
    """把四项探测拼成一句「这个模型适合干什么」的结论。

    只陈述测出来的事实，不猜没测的部分：False 决定「不适合什么」，
    True 汇总「能干什么」，None 不进结论；全部没测成时兜底提醒。
    """
    yes = [lbl for k, lbl in _CAP_LABELS.items()
           if items.get(k) is not None and items[k].supported]
    no = [k for k in _CAP_LABELS
          if items.get(k) is not None and items[k].supported is False]
    parts: list[str] = []
    if yes:
        parts.append("支持：" + "、".join(yes) + "。")
    if "tools" in no:
        parts.append("不支持工具调用：无法使用内置工具与自动化任务，只适合纯对话。")
    if "vision" in no:
        parts.append("不支持图片输入：涉及图片的任务请换模型。")
    if "streaming" in no:
        parts.append("不支持流式输出：回答将整段出现，不能逐字显示。")
    if "reasoning" in no:
        parts.append("不支持思考强度参数：请在该服务上关闭「提供思考强度调节」开关。")
    if not parts:
        return "各项能力都未测出结论，建议检查地址与 Key 后再试。"
    return "".join(parts)


def _cap_client(kind: str, base_url: str | None, api_key: str | None,
                timeout_s: float, transport: object | None):
    """构造探测用的 SDK 客户端（一次构建、四项共用，用完统一关闭）。

    transport 仅供测试注入（httpx.MockTransport）；本地地址不走系统代理，
    与 probe.py 同一套直连规则。httpx 模块按协议取（anthropic 1.5+ 只认 httpx2）。
    """
    from .probe import _is_local, http_client_module

    hx = http_client_module(kind)
    extra: dict = {}
    if transport is not None:
        extra["http_client"] = hx.AsyncClient(transport=transport, timeout=timeout_s)
    elif base_url and _is_local(base_url):
        extra["http_client"] = hx.AsyncClient(trust_env=False, timeout=timeout_s)
    if kind == "anthropic":
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(api_key=api_key or "", base_url=base_url or None,
                              timeout=timeout_s, max_retries=0, **extra)
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key or "", base_url=base_url or None,
                       timeout=timeout_s, max_retries=0, **extra)


async def _probe_openai(client, model: str) -> dict[str, CapabilityItem]:
    """openai 兼容协议的四项探测。"""
    m = model or "gpt-4o-mini"
    out: dict[str, CapabilityItem] = {}

    # 流式：max_tokens=1 的 stream 调用，收到增量即支持
    try:
        stream = await client.chat.completions.create(
            model=m, max_tokens=1,
            messages=[{"role": "user", "content": "hi"}], stream=True,
        )
        async for _ in stream:
            pass
        out["streaming"] = CapabilityItem(True, "流式请求成功返回了增量")
    except Exception as e:  # noqa: BLE001
        out["streaming"] = _cap_item_from_error(e, "流式输出", _FEATURE_MARKERS["streaming"])

    # 工具调用：带一个最小工具定义，看模型是否真的发起 tool_calls
    try:
        resp = await client.chat.completions.create(
            model=m, max_tokens=100,
            messages=[{"role": "user", "content": "请调用 get_weather 工具查询北京天气"}],
            tools=[{"type": "function", "function": {
                "name": "get_weather", "description": "查询一个城市的当前天气",
                "parameters": {"type": "object",
                               "properties": {"city": {"type": "string",
                                                        "description": "城市名"}},
                               "required": ["city"]}}}],
        )
        choice = (resp.choices or [None])[0]
        called = bool(choice and getattr(choice.message, "tool_calls", None))
        if called:
            out["tools"] = CapabilityItem(True, "模型发起了工具调用")
        else:
            out["tools"] = CapabilityItem(
                None, "调用成功但模型没有发起工具调用，无法确认支持")
    except Exception as e:  # noqa: BLE001
        out["tools"] = _cap_item_from_error(e, "工具调用", _FEATURE_MARKERS["tools"])

    # 图片输入：1×1 PNG 的 data URL
    try:
        resp = await client.chat.completions.create(
            model=m, max_tokens=10,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "用两个字回复：收到"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + _TINY_PNG_B64}},
            ]}],
        )
        _ = resp.choices
        out["vision"] = CapabilityItem(True, "服务接受了图片输入")
    except Exception as e:  # noqa: BLE001
        out["vision"] = _cap_item_from_error(e, "图片输入", _FEATURE_MARKERS["vision"])

    # 思考强度：带 reasoning_effort 参数，接受即算支持（是否真实生效以模型为准）
    try:
        resp = await client.chat.completions.create(
            model=m, max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"reasoning_effort": "low"},
        )
        _ = resp.choices
        out["reasoning"] = CapabilityItem(
            True, "服务接受了 reasoning_effort 参数（是否真实生效以模型为准）")
    except Exception as e:  # noqa: BLE001
        out["reasoning"] = _cap_item_from_error(e, "思考强度", _FEATURE_MARKERS["reasoning"])
    return out


async def _probe_anthropic(client, model: str) -> dict[str, CapabilityItem]:
    """anthropic 协议的四项探测。"""
    m = model or "claude-3-haiku-20240307"
    out: dict[str, CapabilityItem] = {}

    try:
        async with client.messages.stream(
            model=m, max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            await stream.get_final_message()
        out["streaming"] = CapabilityItem(True, "流式请求成功返回了增量")
    except Exception as e:  # noqa: BLE001
        out["streaming"] = _cap_item_from_error(e, "流式输出", _FEATURE_MARKERS["streaming"])

    try:
        resp = await client.messages.create(
            model=m, max_tokens=300,
            messages=[{"role": "user", "content": "请调用 get_weather 工具查询北京天气"}],
            tools=[{"name": "get_weather", "description": "查询一个城市的当前天气",
                    "input_schema": {"type": "object",
                                     "properties": {"city": {"type": "string",
                                                              "description": "城市名"}},
                                     "required": ["city"]}}],
        )
        used = resp.stop_reason == "tool_use" or any(
            getattr(b, "type", "") == "tool_use" for b in resp.content)
        if used:
            out["tools"] = CapabilityItem(True, "模型发起了工具调用")
        else:
            out["tools"] = CapabilityItem(
                None, "调用成功但模型没有发起工具调用，无法确认支持")
    except Exception as e:  # noqa: BLE001
        out["tools"] = _cap_item_from_error(e, "工具调用", _FEATURE_MARKERS["tools"])

    try:
        resp = await client.messages.create(
            model=m, max_tokens=10,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "用两个字回复：收到"},
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/png",
                                             "data": _TINY_PNG_B64}},
            ]}],
        )
        _ = resp.content
        out["vision"] = CapabilityItem(True, "服务接受了图片输入")
    except Exception as e:  # noqa: BLE001
        out["vision"] = _cap_item_from_error(e, "图片输入", _FEATURE_MARKERS["vision"])

    try:
        # max_tokens 必须大于 thinking.budget_tokens（anthropic 的硬性规则）
        resp = await client.messages.create(
            model=m, max_tokens=1200,
            messages=[{"role": "user", "content": "hi"}],
            thinking={"type": "enabled", "budget_tokens": 1024},
        )
        _ = resp.content
        out["reasoning"] = CapabilityItem(True, "服务接受了 thinking 参数（扩展思考）")
    except Exception as e:  # noqa: BLE001
        out["reasoning"] = _cap_item_from_error(e, "思考强度", _FEATURE_MARKERS["reasoning"])
    return out


async def probe_capabilities(
    *,
    kind: str = "openai",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str = "",
    timeout_s: float = 20.0,
    transport: object | None = None,
) -> dict:
    """对四项能力各做一次最小真实调用，返回每项三态结论与总结。

    返回：{"kind","model","latency_ms","streaming","tools","vision",
    "reasoning","summary"}，四个能力项各为 {"supported": bool|None,
    "note": str}。本函数不抛异常（SDK 层错误都转成各项的 None + note），
    未知协议除外。
    """
    if kind not in ("openai", "anthropic"):
        raise RuntimeError("未知的协议类型: " + str(kind))
    t0 = time.monotonic()
    try:
        client = _cap_client(kind, (base_url or "").strip() or None, api_key,
                             timeout_s, transport)
    except Exception as e:  # noqa: BLE001 - SDK 构造失败（如无 Key）也返回结构化结果
        diag = diagnose_error(e)
        item = {"supported": None,
                "note": f"没测成：{diag.title}（{str(e).splitlines()[0][:140]}）"}
        return {"kind": kind, "model": (model or "").strip(), "latency_ms": 0,
                "streaming": dict(item), "tools": dict(item),
                "vision": dict(item), "reasoning": dict(item),
                "summary": "各项能力都未测出结论，建议先点「测试连接」确认能对话后再探测。"}
    try:
        if kind == "anthropic":
            items = await _probe_anthropic(client, (model or "").strip())
        else:
            items = await _probe_openai(client, (model or "").strip())
    finally:
        # 客户端持有连接池：无论成败确定性关闭（与 probe/test_connection 同纪律）
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "kind": kind,
        "model": (model or "").strip(),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "streaming": items["streaming"].to_dict(),
        "tools": items["tools"].to_dict(),
        "vision": items["vision"].to_dict(),
        "reasoning": items["reasoning"].to_dict(),
        "summary": capability_summary(items),
    }


# ---- 供应商健康状态 ----

# 状态档位：ok（可用）/ warn（有隐患：余额/限流）/ down（不可用）/ unknown（未测）
HEALTH_LEVELS = ("ok", "warn", "down", "unknown")


@dataclass
class ProviderHealth:
    """一个模型服务的当前健康状态（内存态，重启清零、按次更新）。"""

    name: str
    level: str = "unknown"  # ok | warn | down | unknown
    category: str = ""      # 最近一次错误的分类（level != ok 时有意义）
    title: str = ""
    advice: str = ""
    error: str = ""
    latency_ms: int = 0
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "level": self.level,
            "category": self.category,
            "title": self.title,
            "advice": self.advice,
            "error": self.error[:200] if self.error else "",
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
        }


class HealthRegistry:
    """供应商健康登记：进程内单例（挂 ServerBackend），线程安全靠 asyncio 单线程。"""

    def __init__(self) -> None:
        self._health: dict[str, ProviderHealth] = {}
        # 运行中错峰记录：agent 主循环每次报错时调 record_error
        self._recent_errors: dict[str, float] = {}

    def record_error(self, name: str, diag: ErrorDiagnosis) -> ProviderHealth:
        h = ProviderHealth(
            name=name,
            level="warn" if diag.category in ("rate_limit", "balance") else "down",
            category=diag.category,
            title=diag.title,
            advice=diag.advice,
            error=str(getattr(diag, "status_code", "") or "") + " " + diag.title,
            checked_at=time.time(),
        )
        self._health[name] = h
        self._recent_errors[name] = time.time()
        return h

    def record_ok(self, name: str, latency_ms: int = 0) -> ProviderHealth:
        h = ProviderHealth(
            name=name, level="ok", latency_ms=latency_ms, checked_at=time.time()
        )
        self._health[name] = h
        self._recent_errors.pop(name, None)
        return h

    def set(self, h: ProviderHealth) -> None:
        self._health[h.name] = h

    def get(self, name: str) -> ProviderHealth | None:
        return self._health.get(name)

    def snapshot(self, names: list[str] | None = None) -> list[dict]:
        """全部（或点名）服务的健康状态快照；没测过的补 unknown 占位。"""
        out: list[dict] = []
        for n in (names or list(self._health.keys())):
            h = self._health.get(n)
            if h is None:
                out.append(ProviderHealth(name=n).to_dict())
            else:
                out.append(h.to_dict())
        return out


def base_url_of(cfg_provider) -> str:
    """ProviderConfig 的 base_url（预设缺省时按 kind 给默认）。"""
    url = (getattr(cfg_provider, "base_url", "") or "").strip()
    if url:
        return url
    return "anthropic 默认接口" if getattr(cfg_provider, "kind", "") == "anthropic" else ""
