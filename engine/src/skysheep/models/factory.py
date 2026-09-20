"""根据 SkySheepConfig 构造 Provider 实例。"""

from __future__ import annotations

from ..config import ConfigError, ProviderConfig, resolve_api_key
from .anthropic_provider import AnthropicProvider
from .base import Provider
from .fake import DEMO_DEFAULT_REPLY, DEMO_SCRIPT, FakeProvider
from .openai_compat import OpenAICompatProvider


def build_provider(name: str, cfg: ProviderConfig) -> Provider:
    key = resolve_api_key(name, cfg)
    if key is None:
        # 这段文案直接进前端横幅/报错条：面向的是普通用户，指路设置页而不是配置文件
        raise ConfigError(
            f"模型服务「{name}」还没有配置 API Key——"
            "打开 ⚙ 设置 · 模型服务，选一个服务粘贴 Key 即可"
        )
    if cfg.kind == "fake":
        # 首启向导的「演示模式」：脚本化回放，不访问任何网络服务。
        # demo_mode 标记供后端跳过标题生成之类的附加模型调用。
        provider: Provider = FakeProvider(list(DEMO_SCRIPT)).with_default(DEMO_DEFAULT_REPLY)
        provider.name = "demo"
        provider.model = "演示模式"
        provider.demo_mode = True
        return provider
    if cfg.kind == "anthropic":
        provider: Provider = AnthropicProvider(
            name=name, model=cfg.model, api_key=key, base_url=cfg.base_url, max_tokens=cfg.max_tokens,
            proxy=cfg.proxy,
        )
    else:
        provider = OpenAICompatProvider(
            name=name, model=cfg.model, api_key=key, base_url=cfg.base_url, proxy=cfg.proxy
        )
    # 思考强度：只对声明支持的服务生效（预设/配置里 supports_reasoning=True）
    provider.supports_reasoning = bool(cfg.supports_reasoning)
    provider.set_reasoning_effort(cfg.reasoning_effort)
    # 多模态与采样温度：temperature=None 表示不发送该参数（沿用服务默认）
    provider.supports_vision = bool(cfg.supports_vision)
    provider.temperature = cfg.temperature
    return provider


__all__ = ["build_provider"]
