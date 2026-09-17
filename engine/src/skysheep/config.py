"""SkySheep 配置：~/.skysheep/config.toml + Provider 预设。

Key 解析顺序：config 中的 api_key → 环境变量（env_key 或 <PROVIDER>_API_KEY）。
密钥不写入项目目录，避免误提交。
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ConfigError(Exception):
    pass


def skysheep_home() -> Path:
    env = os.environ.get("SKYSHEEP_HOME")
    return Path(env).expanduser() if env else Path.home() / ".skysheep"


def config_path() -> Path:
    return skysheep_home() / "config.toml"


def db_path() -> Path:
    return skysheep_home() / "skysheep.db"


# 思考强度档位：auto = 不干预（沿用服务默认）；其余档位由各 Provider 映射到自家参数
REASONING_EFFORTS = ("auto", "low", "medium", "high")
REASONING_EFFORT_LABELS = {
    "auto": "自动",
    "low": "低",
    "medium": "中",
    "high": "高",
}


class ProviderConfig(BaseModel):
    # "fake" 仅用于首启向导的「演示模式」：无 Key 也能跑通 Agent 循环
    # （FakeProvider 脚本化回放，见 models/factory.py），不面向真实服务。
    kind: Literal["openai", "anthropic", "fake"] = "openai"
    base_url: str | None = None
    api_key: str | None = None
    env_key: str | None = None
    model: str = ""
    models: list[str] = Field(default_factory=list)  # 已启用的模型列表；model 是当前使用的那个
    max_tokens: int = 8192
    # 该服务的上下文窗口（token）。0 = 用 config.toml 顶层的 context_limit_tokens。
    # 各家窗口差别很大（本地模型常见的只有 8k~32k，云端有 64k/128k/200k），
    # 一刀切会让小窗口模型来不及压缩就撞上限。
    context_limit: int = 0
    # 采样温度。None = 不发送该参数（沿用服务默认，这也是最安全的选择）。
    temperature: float | None = None
    # 该服务的模型是否支持图片输入（多模态）。关掉后贴图/截图会给出可读提示，
    # 而不是把一个看不见的图片塞给纯文本模型换来一句上游报错。
    supports_vision: bool = True
    # 每百万 tokens 单价（元），用于用量统计里的费用估算；0 = 不计价
    price_in: float = 0.0
    price_out: float = 0.0
    # 思考强度：auto 不传参（保持服务默认）；low/medium/high 映射到厂商参数
    reasoning_effort: str = "auto"
    # 是否在界面上提供思考强度控件。默认开启：auto 档不发送任何参数，
    # 因此对"其实不支持该参数"的服务也无副作用；若某服务明确不支持，
    # 可在设置里关掉以隐藏控件（避免给出无效选项）。
    supports_reasoning: bool = True
    # HTTP(S) 代理地址（如 http://127.0.0.1:7890）；空 = 不用代理。
    # 仅作用于该服务的 API 请求（SDK 客户端），不影响系统其他网络访问。
    proxy: str = ""

    def effective_context_limit(self, fallback: int) -> int:
        """该服务实际使用的上下文上限：填了用自己的，否则用全局默认。"""
        return self.context_limit if self.context_limit > 0 else fallback


class RoundtableConfig(BaseModel):
    """圆桌功能：多模型并行独立作答 + 主席融合。"""

    max_members: int = Field(default=3, ge=1, le=8)  # 参与成员上限（不含主席）
    member_timeout_s: int = Field(default=180, ge=10)  # 单成员作答超时
    chair_answers: bool = True  # 主席是否也出一份草稿参与融合


class WebSearchConfig(BaseModel):
    """联网搜索（web_search 工具）的服务商配置。

    provider = auto | bocha | tavily | zhipu | custom；auto 按顺序尝试：
    智谱（复用模型服务的 Zhipu Key，零配置）→ 博查（BOCHA_API_KEY）→ Tavily。
    custom 需自填 base_url 指向自建搜索服务（SearXNG 或任意 REST 接口），
    key 可留空（SearXNG 默认无需鉴权）。
    """

    provider: str = "auto"
    api_key: str = ""
    env_key: str = ""
    base_url: str = ""


class ImageGenConfig(BaseModel):
    """AI 画图（generate_image 工具）的服务商配置。

    provider = auto | zhipu | siliconflow | custom；auto 优先智谱再硅基流动，
    复用模型服务里已配置的 Key。custom 需要自填 base_url（OpenAI 兼容
    /images/generations）。
    """

    provider: str = "auto"
    api_key: str = ""
    env_key: str = ""
    base_url: str = ""
    model: str = ""


class ServerConfig(BaseModel):
    """本地服务绑定、局域网与远程（Tailscale）访问。

    lan=True 时服务绑定 0.0.0.0，手机/局域网设备凭 token 访问；
    tailscale=True 时同样绑定 0.0.0.0，但 HTTP 层只放行本机与 Tailscale
    网段（100.64.0.0/10 及其 IPv6 ULA），手机登录同一 tailnet 即可跨网络访问；
    token 为空时在首次启用时自动生成。默认关闭（只听 127.0.0.1）。
    """

    lan: bool = False
    tailscale: bool = False
    token: str = ""


class SkySheepConfig(BaseModel):
    default: str = "deepseek"
    max_iterations: int = Field(default=40, ge=1, le=200)
    context_limit_tokens: int = Field(default=80_000, ge=4_000)
    compaction_keep_recent: int = Field(default=8, ge=2)
    subagent_enabled: bool = True
    subagent_max_iterations: int = Field(default=25, ge=1, le=100)
    # 只允许工具访问工作目录内的路径（默认关：开着就没法处理目录外的文件）。
    # 只读工具是自动放行的，这个开关是"只看当前项目"的一键闸门。
    restrict_to_workdir: bool = False
    # 电脑控制工具（screenshot/mouse/keyboard/window/clipboard）总开关，默认关：
    # 普通用户用不到，收起入口可以缩小攻击面、降低杀软误报概率。需要时在
    # 设置 · 高级 里打开，热生效。
    computer_control: bool = False
    # 浏览器控制工具（browser：用系统浏览器打开网址/搜索）总开关，默认关，
    # 与电脑控制同一安全姿态；打开后 Agent 可把网页/搜索结果展示给用户看。
    browser_control: bool = False
    # 每日 token 预算护栏：当天累计输入+输出超过该值后拒绝发送新消息（0 = 不限制）。
    # 面向"怕烧钱"的普通用户；用量统计（usage_log）是现成的数据源。
    daily_token_budget: int = Field(default=0, ge=0)
    roundtable: RoundtableConfig = Field(default_factory=RoundtableConfig)
    websearch: WebSearchConfig = Field(default_factory=WebSearchConfig)
    imagegen: ImageGenConfig = Field(default_factory=ImageGenConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


# 内置预设：OpenAI 兼容协议覆盖国内主流与聚合平台，anthropic 为原生协议
PRESETS: dict[str, ProviderConfig] = {
    "deepseek": ProviderConfig(
        kind="openai",
        base_url="https://api.deepseek.com",
        env_key="DEEPSEEK_API_KEY",
        model="deepseek-chat",
        supports_reasoning=True,  # deepseek-reasoner 等型号认 reasoning_effort
    ),
    "zhipu": ProviderConfig(
        kind="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        env_key="ZHIPUAI_API_KEY",
        model="glm-5.3",
    ),
    "moonshot": ProviderConfig(
        kind="openai",
        base_url="https://api.moonshot.cn/v1",
        env_key="MOONSHOT_API_KEY",
        model="kimi-k3",
    ),
    "openrouter": ProviderConfig(
        kind="openai",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        model="anthropic/claude-opus-5",
        supports_reasoning=True,
    ),
    "siliconflow": ProviderConfig(
        kind="openai",
        base_url="https://api.siliconflow.cn/v1",
        env_key="SILICONFLOW_API_KEY",
        model="deepseek-ai/DeepSeek-V3.2",
        supports_reasoning=True,
    ),
    "anthropic": ProviderConfig(
        kind="anthropic",
        env_key="ANTHROPIC_API_KEY",
        model="claude-opus-5",
        supports_reasoning=True,  # 映射为 thinking.budget_tokens
    ),
    "ollama": ProviderConfig(
        kind="openai",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model="qwen3:8b",
    ),
}

# 预设服务的「注册/控制台」入口：首启向导里引导新用户去拿 API Key。
# 只做展示链接，不参与任何请求逻辑。
PRESET_SIGNUP_URLS: dict[str, str] = {
    "deepseek": "https://platform.deepseek.com/",
    "zhipu": "https://open.bigmodel.cn/",
    "moonshot": "https://platform.moonshot.cn/",
    "openrouter": "https://openrouter.ai/settings/keys",
    "siliconflow": "https://cloud.siliconflow.cn/account/ak",
    "anthropic": "https://console.anthropic.com/settings/keys",
}


def load_config() -> SkySheepConfig:
    """读取 config.toml 并与预设合并（文件条目按字段覆盖预设）。

    config 里的 disabled_providers 列出"从列表里删掉"的服务；内置预设删掉后
    仍然每次都会合并回来，所以必须先合并再按名单剔除。
    """
    merged = {k: v.model_copy() for k, v in PRESETS.items()}
    default = "deepseek"
    max_iterations = 40
    disabled: list[str] = []
    roundtable = RoundtableConfig()
    websearch = WebSearchConfig()
    imagegen = ImageGenConfig()
    server = ServerConfig()
    p = config_path()
    if p.exists():
        try:
            with open(p, "rb") as f:
                raw = tomllib.load(f)
        except Exception as e:
            raise ConfigError(f"failed to parse {p}: {e}") from e
        default = raw.get("default", default)
        max_iterations = int(raw.get("max_iterations", max_iterations))
        context_limit = int(raw.get("context_limit_tokens", 80_000))
        keep_recent = int(raw.get("compaction_keep_recent", 8))
        sub_enabled = bool(raw.get("subagent_enabled", True))
        sub_iters = int(raw.get("subagent_max_iterations", 25))
        restrict_workdir = bool(raw.get("restrict_to_workdir", False))
        computer_control = bool(raw.get("computer_control", False))
        browser_control = bool(raw.get("browser_control", False))
        daily_budget = max(0, int(raw.get("daily_token_budget", 0)))
        disabled = [str(n) for n in (raw.get("disabled_providers") or [])]
        rt_raw = raw.get("roundtable")
        if isinstance(rt_raw, dict):
            roundtable = RoundtableConfig(
                **{k: v for k, v in rt_raw.items() if k in RoundtableConfig.model_fields}
            )
        for section_name, model_cls, cur in (
            ("websearch", WebSearchConfig, websearch),
            ("imagegen", ImageGenConfig, imagegen),
            ("server", ServerConfig, server),
        ):
            section = raw.get(section_name)
            if isinstance(section, dict):
                cur = model_cls(**{k: v for k, v in section.items() if k in model_cls.model_fields})
            if section_name == "websearch":
                websearch = cur
            elif section_name == "imagegen":
                imagegen = cur
            else:
                server = cur
        for name, section in (raw.get("providers") or {}).items():
            if not isinstance(section, dict):
                continue
            if name in merged:
                data = merged[name].model_dump()
                data.update({k: v for k, v in section.items() if v is not None})
                merged[name] = ProviderConfig(**data)
            else:
                merged[name] = ProviderConfig(**section)
    else:
        context_limit = 80_000
        keep_recent = 8
        sub_enabled = True
        sub_iters = 25
        restrict_workdir = False
        computer_control = False
        browser_control = False
        daily_budget = 0
    for name in disabled:
        merged.pop(name, None)
    # 老配置只有 model 没有 models：把当前模型视为已启用列表的首个成员
    for pc in merged.values():
        if not pc.models and pc.model:
            pc.models = [pc.model]
        if not pc.model and pc.models:
            pc.model = pc.models[0]
    # default 必须落在可用列表里：被删掉或写错名字都不能让启动时的 provider 构建炸掉
    if default not in merged:
        default = "deepseek" if "deepseek" in merged else next(iter(merged), default)
    return SkySheepConfig(
        default=default,
        max_iterations=max_iterations,
        context_limit_tokens=context_limit,
        compaction_keep_recent=keep_recent,
        subagent_enabled=sub_enabled,
        subagent_max_iterations=sub_iters,
        restrict_to_workdir=restrict_workdir,
        computer_control=computer_control,
        browser_control=browser_control,
        daily_token_budget=daily_budget,
        roundtable=roundtable,
        websearch=websearch,
        imagegen=imagegen,
        server=server,
        providers=merged,
    )


def set_advanced_settings_in_config(
    *,
    max_iterations: int | None = None,
    context_limit_tokens: int | None = None,
    compaction_keep_recent: int | None = None,
    restrict_to_workdir: bool | None = None,
    computer_control: bool | None = None,
    browser_control: bool | None = None,
    daily_token_budget: int | None = None,
) -> None:
    """写入「高级」设置（config.toml 顶层；None 表示该项不动）。"""
    if max_iterations is not None and not 1 <= int(max_iterations) <= 200:
        raise ConfigError("主循环最大轮数要在 1–200 之间")
    if context_limit_tokens is not None and not 4_000 <= int(context_limit_tokens) <= 2_000_000:
        raise ConfigError("上下文上限要在 4000–2000000 tokens 之间")
    if compaction_keep_recent is not None and not 2 <= int(compaction_keep_recent) <= 100:
        raise ConfigError("压缩保留条数要在 2–100 之间")
    if daily_token_budget is not None and int(daily_token_budget) > 100_000_000:
        raise ConfigError("每日 token 预算过大，请填写 0（不限制）到 1 亿之间的整数")
    p, raw = _read_raw_config()
    if max_iterations is not None:
        raw["max_iterations"] = int(max_iterations)
    if context_limit_tokens is not None:
        raw["context_limit_tokens"] = int(context_limit_tokens)
    if compaction_keep_recent is not None:
        raw["compaction_keep_recent"] = int(compaction_keep_recent)
    if restrict_to_workdir is not None:
        raw["restrict_to_workdir"] = bool(restrict_to_workdir)
    if computer_control is not None:
        raw["computer_control"] = bool(computer_control)
    if browser_control is not None:
        raw["browser_control"] = bool(browser_control)
    if daily_token_budget is not None:
        raw["daily_token_budget"] = max(0, int(daily_token_budget))
    _write_raw_config(p, raw)


def set_subagent_settings_in_config(
    *, enabled: bool | None = None, max_iterations: int | None = None
) -> None:
    """写入子代理设置（config.toml 顶层；None 表示该项不动）。"""
    if max_iterations is not None and not 1 <= int(max_iterations) <= 100:
        raise ConfigError("子代理迭代轮数要在 1–100 之间")
    p, raw = _read_raw_config()
    if enabled is not None:
        raw["subagent_enabled"] = bool(enabled)
    if max_iterations is not None:
        raw["subagent_max_iterations"] = int(max_iterations)
    _write_raw_config(p, raw)


def resolve_api_key(name: str, cfg: ProviderConfig) -> str | None:
    if cfg.api_key:
        return cfg.api_key
    env_name = cfg.env_key or (name + "_API_KEY").upper()
    return os.environ.get(env_name)


def _read_raw_config() -> tuple[Path, dict]:
    """读取 config.toml 原始数据（含目录边界校验）。"""
    # 目录边界校验：配置写入只允许发生在 SkySheep 主目录内
    # （防 SKYSHEEP_HOME 环境变量被指向任意位置后在此写文件）
    home = skysheep_home().resolve()
    p = config_path().resolve()
    if p.parent != home:
        raise ConfigError("config path escapes SkySheep home: " + str(p))

    raw: dict = {}
    if p.exists():
        try:
            with p.open("rb") as f:
                raw = tomllib.load(f)
        except Exception as e:
            raise ConfigError(f"failed to parse {p}: {e}") from e
    return p, raw


def _write_raw_config(p: Path, raw: dict) -> None:
    import tomli_w

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomli_w.dumps(raw), encoding="utf-8")


def update_provider_in_config(
    name: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    kind: str | None = None,
    set_default: bool = False,
    reasoning_effort: str | None = None,
    supports_reasoning: bool | None = None,
    supports_vision: bool | None = None,
    context_limit: int | None = None,
    temperature: float | str | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    proxy: str | None = None,
) -> None:
    """把一个 provider 的字段写回 ~/.skysheep/config.toml（整体重写，注释会丢失）。

    只更新传入的字段；api_key 传 None 表示保持不变。
    temperature 传空串表示"清除该设置"（回到不发送该参数）。
    """
    p, raw = _read_raw_config()

    providers = raw.setdefault("providers", {})
    section = dict(providers.get(name) or {})
    if kind is not None:
        section["kind"] = kind
    if base_url is not None:
        section["base_url"] = base_url
    if model is not None:
        section["model"] = model
    if api_key is not None:
        section["api_key"] = api_key
    if reasoning_effort is not None:
        effort = str(reasoning_effort).strip().lower()
        if effort not in REASONING_EFFORTS:
            raise ConfigError("思考强度只支持 " + " / ".join(REASONING_EFFORTS) + ": " + effort)
        section["reasoning_effort"] = effort
    if supports_reasoning is not None:
        section["supports_reasoning"] = bool(supports_reasoning)
    if supports_vision is not None:
        section["supports_vision"] = bool(supports_vision)
    if context_limit is not None:
        limit = int(context_limit)
        if limit and not 4_000 <= limit <= 2_000_000:
            raise ConfigError("上下文上限要在 4000–2000000 tokens 之间（0 = 用全局默认）")
        section["context_limit"] = limit
    if temperature is not None:
        if isinstance(temperature, str) and not temperature.strip():
            section.pop("temperature", None)  # 空 = 交回服务默认
        else:
            try:
                temp = float(temperature)
            except (TypeError, ValueError):
                raise ConfigError("温度需要是 0–2 之间的数字（留空 = 用服务默认）") from None
            if not 0.0 <= temp <= 2.0:
                raise ConfigError("温度要在 0–2 之间（留空 = 用服务默认）")
            section["temperature"] = temp
    if price_in is not None:
        section["price_in"] = max(0.0, float(price_in))
    if price_out is not None:
        section["price_out"] = max(0.0, float(price_out))
    if proxy is not None:
        proxy = str(proxy).strip()
        if proxy:
            from urllib.parse import urlparse

            if urlparse(proxy).scheme not in ("http", "https", "socks5"):
                raise ConfigError("代理地址需以 http:// 或 https:// 或 socks5:// 开头: " + proxy)
        section["proxy"] = proxy
    providers[name] = section
    if set_default:
        raw["default"] = name
    if not section and not set_default:
        raise ConfigError("no fields to save for provider: " + name)

    _write_raw_config(p, raw)


PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def validate_new_provider_name(name: str) -> str:
    """校验自定义 provider 名称：合法的 toml 表名片段，且不与内置/现有服务重名。"""
    name = (name or "").strip()
    if not PROVIDER_NAME_RE.match(name):
        raise ConfigError(
            "名称只能用字母、数字、下划线、短横线（2-40 字符，需以字母或数字开头）: " + (name or "(空)")
        )
    if name in PRESETS:
        if name in disabled_providers_in_config():
            raise ConfigError(
                f"「{name}」是已被删除的内置服务，在下面「已删除的内置服务」里点恢复即可，不用重新添加"
            )
        raise ConfigError(f"「{name}」是内置服务名，请换一个名称")
    return name


def add_provider_to_config(
    name: str,
    *,
    kind: str = "openai",
    base_url: str = "",
    model: str = "",
    api_key: str | None = None,
    set_default: bool = False,
    models: list[str] | None = None,
) -> None:
    """新增一个自定义 provider（写入 config.toml，重启或热更新后可用）。

    models 为「添加时一并启用」的模型列表（可多选）：当前模型 model 始终 ∈ models；
    不传则退化为单模型（models=[model]），与老行为一致。
    """
    name = validate_new_provider_name(name)
    if kind not in ("openai", "anthropic"):
        raise ConfigError("协议类型只能是 openai（OpenAI 兼容）或 anthropic: " + str(kind))
    model = (model or "").strip()
    if not model:
        raise ConfigError("请填写模型名（如 deepseek-chat / gpt-4o）")
    base_url = (base_url or "").strip().rstrip("/")
    if kind == "openai":
        if not base_url:
            raise ConfigError("OpenAI 兼容服务需要填写接口地址（base_url）")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ConfigError("接口地址需以 http:// 或 https:// 开头: " + base_url)

    # 一并启用的模型：去空、去重（保序），当前模型恒在首位
    others: list[str] = []
    for m in (models or []):
        m = str(m).strip()
        if m and m != model and m not in others:
            others.append(m)
    enabled = [model] + others

    p, raw = _read_raw_config()
    providers = raw.setdefault("providers", {})
    if name in providers:
        raise ConfigError(f"服务「{name}」已存在，请在列表里直接编辑它")

    section: dict = {"kind": kind, "model": model, "models": enabled}
    if base_url:
        section["base_url"] = base_url
    if api_key:
        section["api_key"] = api_key
    providers[name] = section
    if set_default:
        raw["default"] = name
    _write_raw_config(p, raw)


def disabled_providers_in_config() -> list[str]:
    """当前被隐藏（从列表里删掉）的服务名。"""
    p = config_path()
    if not p.exists():
        return []
    try:
        with p.open("rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return []
    return [str(n) for n in (raw.get("disabled_providers") or [])]


def _drop_default_if_matches(raw: dict, name: str) -> None:
    """删掉的正好是默认项时把默认指向一个还在的服务，避免启动时 default 悬空。"""
    if raw.get("default") != name:
        return
    still_there = set(PRESETS) | set(raw.get("providers") or {})
    still_there -= set(raw.get("disabled_providers") or [])
    still_there.discard(name)
    if "deepseek" in still_there:
        raw["default"] = "deepseek"
    elif still_there:
        raw["default"] = sorted(still_there)[0]
    else:
        raw["default"] = name


def disable_provider_in_config(name: str) -> None:
    """停用一个服务（配置数据保留，只从列表里隐藏），可随时恢复。

    与 remove_provider_from_config 的区别：这个对自定义服务也只进名单、
    不删 [providers.<name>] 段，重新启用后配置原样回来。
    """
    if name in disabled_providers_in_config():
        raise ConfigError(f"「{name}」已经停用了")
    p, raw = _read_raw_config()
    if name not in PRESETS and name not in (raw.get("providers") or {}):
        raise ConfigError("模型服务不存在: " + name)
    disabled = [str(n) for n in (raw.get("disabled_providers") or [])]
    raw["disabled_providers"] = [*disabled, name]
    _drop_default_if_matches(raw, name)
    _write_raw_config(p, raw)


def enabled_models_of(name: str) -> list[str]:
    """读取某服务当前已启用的模型列表（老配置自动从 model 字段迁移）。"""
    p = config_path()
    if not p.exists():
        return []
    try:
        with p.open("rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return []
    section = (raw.get("providers") or {}).get(name) or {}
    models = [str(m) for m in (section.get("models") or [])]
    if not models and section.get("model"):
        models = [str(section["model"])]
    return models


def set_provider_models_in_config(name: str, models: list[str]) -> None:
    """整体写入某服务的已启用模型列表（model 字段不动，由保存/设默认负责）。"""
    p, raw = _read_raw_config()
    providers = raw.setdefault("providers", {})
    if name in PRESETS and name not in providers:
        providers[name] = {}
    section = providers.setdefault(name, {})
    section["models"] = [str(m) for m in models]
    # 保持「当前模型 ∈ 已启用列表」的不变量：当前模型被删时换到列表第一个
    if section.get("model") and section["model"] not in section["models"]:
        section["model"] = section["models"][0]
    _write_raw_config(p, raw)


def remove_provider_from_config(name: str) -> None:
    """从模型服务列表里删掉一个服务。

    自定义服务：直接删掉 config 里的 [providers.<name>] 段。
    内置服务：记入 config 的 disabled_providers 名单（预设每次启动都会合并回来，
    所以必须用名单屏蔽），可随时用 restore_provider_in_config 恢复。
    """
    p, raw = _read_raw_config()
    providers = raw.get("providers") or {}
    disabled = [str(n) for n in (raw.get("disabled_providers") or [])]

    if name in PRESETS:
        if name in disabled:
            raise ConfigError(f"「{name}」已经被隐藏了")
        raw["disabled_providers"] = [*disabled, name]
    else:
        if name not in providers:
            raise ConfigError("模型服务不存在: " + name)
        del providers[name]
        if disabled:
            raw["disabled_providers"] = [n for n in disabled if n != name]

    _drop_default_if_matches(raw, name)
    _write_raw_config(p, raw)


def restore_provider_in_config(name: str) -> None:
    """把服务放回列表。

    内置服务：按当前出厂预设重建条目（kind/base_url/env_key/model 取最新
    PRESETS 值），仅保留 api_key（用户填过优先，否则用出厂值如 ollama）。
    隐藏内置服务时旧条目仍留在文件里，若原样放行，历史快照（如旧默认模型名）
    会覆盖升级后的预设，导致「恢复回来还是老模型」。
    自定义服务：仅移出停用名单，条目原样保留（set_provider_enabled 重新启用时走这里）。
    """
    p, raw = _read_raw_config()
    disabled = [str(n) for n in (raw.get("disabled_providers") or [])]
    if name not in disabled:
        raise ConfigError("该服务没有被隐藏: " + name)
    remaining = [n for n in disabled if n != name]
    if remaining:
        raw["disabled_providers"] = remaining
    else:
        raw.pop("disabled_providers", None)
    if name in PRESETS:
        preset = PRESETS[name].model_dump()
        section = {
            k: preset[k]
            for k in ("kind", "base_url", "env_key", "model")
            if preset[k] is not None
        }
        old = (raw.get("providers") or {}).get(name) or {}
        api_key = old.get("api_key") or preset["api_key"]
        if api_key:
            section["api_key"] = api_key
        raw.setdefault("providers", {})[name] = section
    _write_raw_config(p, raw)


CONFIG_TEMPLATE = """\
# SkySheep 配置
# 位置: ~/.skysheep/config.toml
# API Key 推荐放在环境变量中（如 DEEPSEEK_API_KEY），
# 也可以直接写在下面 provider 的 api_key 字段里（注意不要提交到 git）。

# 默认使用的 provider 名称
default = "deepseek"

# 单次任务最大工具调用轮数
max_iterations = 40

[providers.deepseek]
kind = "openai"
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
# api_key = "sk-..."          # 或设置环境变量 DEEPSEEK_API_KEY

[providers.zhipu]
kind = "openai"
base_url = "https://open.bigmodel.cn/api/paas/v4"
model = "glm-5.3"
# api_key = "..."              # 或设置环境变量 ZHIPUAI_API_KEY

[providers.anthropic]
kind = "anthropic"
model = "claude-opus-5"
# api_key = "..."              # 或设置环境变量 ANTHROPIC_API_KEY

[providers.ollama]
kind = "openai"
base_url = "http://localhost:11434/v1"
api_key = "ollama"
model = "qwen3:8b"

# 也可以自定义任意 OpenAI 兼容 provider：
# [providers.siliconflow]
# kind = "openai"
# base_url = "https://api.siliconflow.cn/v1"
# model = "deepseek-ai/DeepSeek-V3.2"
# env_key = "SILICONFLOW_API_KEY"
"""


def write_config_template(force: bool = False) -> Path:
    p = config_path()
    if p.exists() and not force:
        raise ConfigError("config already exists: " + str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return p


def update_config_section(section: str, updates: dict) -> dict:
    """写 config.toml 的一个普通 section（[websearch] / [imagegen] / [server]）。

    updates 里值为空串的键表示删除该字段（恢复默认）；返回写完的 section 内容。
    """
    p, raw = _read_raw_config()
    cur = dict(raw.get(section) or {})
    for k, v in (updates or {}).items():
        if v is None:
            continue
        if v == "":
            cur.pop(k, None)
        else:
            cur[k] = v
    if cur:
        raw[section] = cur
    else:
        raw.pop(section, None)
    _write_raw_config(p, raw)
    return cur


_WEBSEARCH_AUTO_ORDER = ("zhipu", "bocha", "tavily")


def resolve_websearch(cfg: SkySheepConfig) -> dict | None:
    """解析联网搜索用哪个服务商 + Key；一个都配不出时返回 None。

    显式 provider：Key 依次取 配置里的 api_key → env_key → 默认环境变量
    （<PROVIDER>_API_KEY）；zhipu 再回落到模型服务里已配置的 Zhipu Key。
    auto：智谱（复用 Zhipu Key，零配置）→ 博查 → Tavily。
    custom：只认自填的 base_url，Key 可留空（自建 SearXNG 默认无需鉴权）；
    custom 不参与 auto 档，因为只有用户自己知道自建服务的地址。
    """
    ws = cfg.websearch
    if ws.provider == "custom":
        if not ws.base_url.strip():
            return None
        key = ws.api_key or (os.environ.get(ws.env_key) if ws.env_key else "")
        return {"provider": "custom", "api_key": key, "base_url": ws.base_url.strip()}
    order = _WEBSEARCH_AUTO_ORDER if ws.provider == "auto" else (ws.provider,)
    if ws.provider != "auto" and ws.provider not in _WEBSEARCH_AUTO_ORDER:
        return None
    for name in order:
        default_env = {"bocha": "BOCHA_API_KEY", "tavily": "TAVILY_API_KEY", "zhipu": "ZHIPUAI_API_KEY"}
        key = ws.api_key or (os.environ.get(ws.env_key) if ws.env_key else "")
        if not key and name == ws.provider:
            key = os.environ.get(default_env[name], "")
        # zhipu：复用模型服务里配置好的 Key（写死 env_key 或 api_key 的都算）
        if not key and name == "zhipu":
            pc = cfg.providers.get("zhipu")
            if pc is not None:
                key = resolve_api_key("zhipu", pc) or ""
        if not key:
            key = os.environ.get(default_env[name], "") if name != ws.provider else ""
        if key:
            return {"provider": name, "api_key": key}
    return None


def resolve_imagegen(cfg: SkySheepConfig) -> dict | None:
    """解析画图用哪个服务商 + Key + 模型；一个都配不出时返回 None。

    auto：智谱（cogview-3-flash）→ 硅基流动（Kolors），Key 复用模型服务配置；
    custom：必须显式填 base_url（OpenAI 兼容 /images/generations）。
    """
    ig = cfg.imagegen
    if ig.provider == "custom":
        key = ig.api_key or (os.environ.get(ig.env_key) if ig.env_key else "")
        if ig.base_url and key:
            return {
                "provider": "custom", "api_key": key,
                "base_url": ig.base_url, "model": ig.model,
            }
        return None
    order = (("zhipu", "cogview-3-flash"), ("siliconflow", "Kwai-Kolors/Kolors")) \
        if ig.provider == "auto" else ((ig.provider, ig.model),)
    for name, default_model in order:
        if name not in ("zhipu", "siliconflow"):
            continue
        key = ig.api_key or (os.environ.get(ig.env_key) if ig.env_key else "")
        if not key:
            pc = cfg.providers.get(name)
            if pc is not None:
                key = resolve_api_key(name, pc) or ""
        if not key and name == ig.provider:
            key = os.environ.get(
                "ZHIPUAI_API_KEY" if name == "zhipu" else "SILICONFLOW_API_KEY", "")
        if key:
            model = (ig.model if name == ig.provider and ig.model else "") or default_model
            return {"provider": name, "api_key": key, "base_url": "", "model": model}
    return None
