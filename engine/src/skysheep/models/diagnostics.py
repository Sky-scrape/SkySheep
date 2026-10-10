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
        "查看速率与配额档位，或更换其他已配置的模型服务。",
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
        "确认当前账号有该模型的访问权限（部分模型需要申请）。",
    ),
    "network": (
        "网络连接失败",
        "检查网络与代理设置：该服务走代理时确认代理已启动且端口正确；"
        "本地服务（Ollama 等）需要直连，若开了代理请把 localhost 加入直连名单。",
    ),
    "server": (
        "模型服务暂时不可用",
        "上游服务正在异常或过载，通常稍等即恢复；持续出现时到服务商状态页确认，"
        "或先切换到其他已配置的模型服务。",
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
) -> TestResult:
    """对单个模型服务做一次最小真实调用，返回连通性与能力探测结果。

    流程：models.list 找模型拿窗口字段 → 用 1 token 的非流式请求实测对话。
    models.list 失败不提前失败（部分服务不提供该接口）：仍会尝试对话调用。
    对话调用成功即判定连通；失败时给出分类诊断。
    """
    from .probe import _extract_context_limit, _is_local

    base = (base_url or "").strip()
    model = (model or "").strip()
    result = TestResult(ok=False, model=model)
    extra: dict = {}
    if base and _is_local(base):
        # 本地服务直连（probe 同款：系统代理会把 127.0.0.1 转走）
        extra["http_client"] = __import__("httpx").AsyncClient(
            trust_env=False, timeout=timeout_s
        )

    t0 = time.monotonic()

    # 1) 模型列表：确认 Key/地址可用 + 收集窗口字段（失败不提前失败）
    model_listed: bool | None = None
    ctx_limit: int | None = None
    try:
        if kind == "anthropic":
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(
                api_key=api_key or "", base_url=base or None,
                timeout=timeout_s, max_retries=0, **extra,
            )
        else:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=api_key or "", base_url=base or None,
                timeout=timeout_s, max_retries=0, **extra,
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
                import httpx as _httpx

                url = (base or "https://api.openai.com/v1").rstrip("/") + "/models"
                headers = (
                    {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
                    if kind == "anthropic"
                    else {"Authorization": "Bearer " + (api_key or "")}
                )
                async with _httpx.AsyncClient(
                    timeout=timeout_s, trust_env=not (base and _is_local(base))
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
                timeout=timeout_s, max_retries=0, **extra,
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
                timeout=timeout_s, max_retries=0, **extra,
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
