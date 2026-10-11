"""模型服务管理与诊断：设置页服务清单、增删改、探针检测、测试连接与备用模型。

从 backend.py 按职责注释整段搬入。方法体逐字保留，逻辑不变。
覆盖模型服务的管理面：设置页清单（providers_detail）、增删改与启用/停用、
模型与上下文探测（probe_models / probe_context）、服务保存（save_provider）、
测试连接与健康状态（model_test_connection / providers_health）、备用模型
（_fallback_target / _switch_to_fallback）；_mask_key 为同区 Key 掩码助手，
webtools 的设置卡共用。
"""

from __future__ import annotations

from ...config import (
    PRESETS,
    ConfigError,
    add_provider_to_config,
    config_path,
    db_path,
    disable_provider_in_config,
    disabled_providers_in_config,
    load_config,
    remove_provider_from_config,
    resolve_api_key,
    restore_provider_in_config,
    skysheep_home,
    update_provider_in_config,
)
from ...models.diagnostics import probe_capabilities, test_connection
from ...models.probe import probe_context_limit, probe_provider_models
from ...obs import info as obs_info
from ...obs import warning as obs_warning


class ProvidersMixin:
    """模型服务与诊断（设置页服务清单 / 增删改 / 探针 / 健康 / 备用模型）。

    方法自 backend.py 按职责注释逐字搬入。
    """

    @staticmethod
    def _mask_key(key: str | None) -> str:
        if not key:
            return ""
        if len(key) <= 10:
            return "已设置"
        return key[:6] + "…" + key[-4:]

    async def providers_detail(self) -> dict:
        """设置页的模型服务清单（Key 只回传掩码，绝不回传全文）。"""
        detail = {}
        for name, pc in self.cfg.providers.items():
            key = resolve_api_key(name, pc)
            detail[name] = {
                "kind": pc.kind,
                "label": pc.label or name,  # 界面显示名（预设如「智谱」「小米 Mimo」）
                "base_url": pc.base_url or "",
                "model": pc.model,
                "has_key": key is not None,
                "key_mask": self._mask_key(key),
                "key_from_env": bool(key and not pc.api_key),
                "is_default": name == self.cfg.default,
                "is_active": name == self.provider_name,
                "active_model": self.provider_model if name == self.provider_name else "",
                # 内置预设不可删除：删掉 config 里的条目它也仍会被合并回来
                "is_preset": name in PRESETS,
                "models": list(pc.models),
                "supports_reasoning": bool(pc.supports_reasoning),
                "supports_vision": bool(pc.supports_vision),
                "reasoning_effort": pc.reasoning_effort,
                "context_limit": pc.context_limit,
                "effective_context_limit": pc.effective_context_limit(
                    self.cfg.context_limit_tokens
                ),
                "global_context_limit": self.cfg.context_limit_tokens,
                "temperature": pc.temperature,
                "proxy": pc.proxy or "",
                "price_in": pc.price_in,
                "price_out": pc.price_out,
                "price_cache": pc.price_cache,
            }
        return {
            "config_path": str(config_path()),
            "home_dir": str(skysheep_home()),
            "db_path": str(db_path()),
            "default": self.cfg.default,
            "active": self.provider_name,
            "provider_error": self.provider_error,
            "disabled": disabled_providers_in_config(),  # 已隐藏的内置服务，可恢复
            "providers": detail,
        }

    async def add_provider(
        self,
        name: str,
        *,
        kind: str = "openai",
        base_url: str = "",
        model: str = "",
        api_key: str | None = None,
        set_default: bool = False,
        models: list[str] | None = None,
    ) -> dict:
        """新增自定义模型服务（写入 config.toml）；当前没有可用模型时顺带热启用。

        models 为添加时一并启用的模型列表（可多选），model 是其中当前使用的那个。
        """
        try:
            add_provider_to_config(
                name,
                kind=kind,
                base_url=base_url,
                model=model,
                api_key=api_key or None,
                set_default=set_default,
                models=models,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        clean = name.strip()
        activated = False
        # 缺模型时顺手把新服务用起来；已有可用模型就不打断用户当前选择
        if self.provider is None:
            try:
                self.provider = self._build_provider(clean)
                self.provider_error = None
                for ag in self._for_each_agent():
                    ag.provider = self.provider
                activated = True
            except RuntimeError as e:
                self.provider_error = str(e)
        return {
            "added": clean,
            "activated": activated,
            "provider_error": self.provider_error,
            "model": getattr(self.provider, "model", "") if activated else "",
        }

    async def delete_provider(self, name: str) -> dict:
        """从列表里删掉模型服务：自定义服务彻底删除，内置服务改为隐藏（可恢复）。"""
        try:
            remove_provider_from_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        was_active = name == self.provider_name
        if was_active:
            self.provider = None
            self.provider_name = ""
            # 让界面说明白"为什么现在没有模型可用"，而不是显示空的 provider/model
            self.provider_error = f"已删除正在使用的模型服务「{name}」，请重新选择一个"
        return {
            "removed": name,
            "default": self.cfg.default,
            "was_active": was_active,
            "hidden": name in PRESETS,  # 内置服务只是隐藏，可恢复
        }

    async def restore_provider(self, name: str) -> dict:
        """把隐藏的服务放回模型服务列表（内置服务按最新出厂预设重建）。"""
        try:
            restore_provider_in_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        return {"restored": name}

    async def set_provider_enabled(self, name: str, enabled: bool) -> dict:
        """启用/停用一个模型服务（停用只隐藏，配置数据保留）。"""
        try:
            if enabled:
                restore_provider_in_config(name)
            else:
                disable_provider_in_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        was_active = not enabled and name == self.provider_name
        if was_active:
            self.provider = None
            self.provider_name = ""
            self.provider_error = f"已停用正在使用的模型服务「{name}」，请重新选择一个"
        return {"name": name, "enabled": enabled, "was_active": was_active}

    async def probe_models(
        self,
        *,
        name: str = "",
        kind: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> dict:
        """检测某个模型服务当前可用的模型名（用于界面里一键选取，免手输）。

        界面上可能改了地址/Key 还没保存，所以优先用传入的值；没传的部分
        回落到该服务已保存的配置（含环境变量里的 Key）。
        """
        pc = self.cfg.providers.get(name) if name else None
        eff_kind = (kind or (pc.kind if pc else "openai")).strip()
        eff_url = (base_url or (pc.base_url if pc else "") or "").strip()
        eff_key = (api_key or "").strip()
        if not eff_key and pc is not None:
            eff_key = resolve_api_key(name, pc) or ""

        models = await probe_provider_models(
            kind=eff_kind, base_url=eff_url or None, api_key=eff_key or None
        )
        return {
            "provider": name,
            "kind": eff_kind,
            "base_url": eff_url,
            "count": len(models),
            "models": models,
        }

    async def probe_context(
        self,
        *,
        name: str = "",
        kind: str = "",
        base_url: str = "",
        api_key: str = "",
        model: str = "",
    ) -> dict:
        """探测某个模型服务的上下文窗口上限（供「上下文上限」一键填入）。

        与 probe_models 同一套取值口径：界面改了还没保存的地址/Key 优先，
        没传的部分回落到已保存配置（含环境变量里的 Key）；模型名同样优先
        用传入值，回落到该服务当前启用的模型。
        """
        pc = self.cfg.providers.get(name) if name else None
        eff_kind = (kind or (pc.kind if pc else "openai")).strip()
        eff_url = (base_url or (pc.base_url if pc else "") or "").strip()
        eff_key = (api_key or "").strip()
        if not eff_key and pc is not None:
            eff_key = resolve_api_key(name, pc) or ""
        eff_model = (model or (pc.model if pc else "") or "").strip()

        out = await probe_context_limit(
            kind=eff_kind, base_url=eff_url or None, api_key=eff_key or None, model=eff_model
        )
        return {"provider": name, "kind": eff_kind, "model": eff_model, **out}

    async def save_provider(
        self,
        name: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        kind: str | None = None,
        set_default: bool = False,
        supports_reasoning: bool | None = None,
        supports_vision: bool | None = None,
        context_limit: int | None = None,
        temperature: float | str | None = None,
        price_in: float | None = None,
        price_out: float | None = None,
        price_cache: float | None = None,
        proxy: str | None = None,
    ) -> dict:
        """保存 provider 字段到 config.toml 并热生效（若是当前使用的模型）。"""
        if name not in self.cfg.providers:
            raise RuntimeError("unknown provider: " + name)
        # 设为默认时把「当前正在用的模型」也固化进 config，下次启动即用它
        if set_default and not model and name == self.provider_name and self.provider_model:
            model = self.provider_model
        update_provider_in_config(
            name,
            base_url=(base_url or None),
            model=(model or None),
            api_key=(api_key or None),
            kind=(kind or None),
            set_default=set_default,
            supports_reasoning=supports_reasoning,
            supports_vision=supports_vision,
            context_limit=context_limit,
            temperature=temperature,
            price_in=price_in,
            price_out=price_out,
            price_cache=price_cache,
            proxy=proxy,
        )
        self.cfg = load_config()
        rebuilt = False
        if name == self.provider_name or self.provider is None:
            try:
                self.provider = self._build_provider(name)
                self.provider_error = None
                for ag in self._for_each_agent():
                    ag.provider = self.provider
                rebuilt = True
            except RuntimeError as e:
                self.provider_error = str(e)
        # 上下文上限跟服务走：改了当前服务的上限要立刻作用到活着的 Agent
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.context_limit_tokens = limit
        return {
            "saved": name,
            "rebuilt": rebuilt,
            "provider_error": self.provider_error,
            "model": getattr(self.provider, "model", ""),
            "reasoning": self.reasoning_state(),
        }

    # ---- 模型诊断（models/diagnostics.py）：测试连接 / 健康状态 / 备用模型 ----

    async def model_test_connection(self, params: dict) -> dict:
        """测试连接：对指定服务做一次最小真实调用（非流式 1 token）。

        与 probe_models 同一套取值口径：界面改了还没保存的地址/Key 优先，
        没传的部分回落到已保存配置（含环境变量里的 Key）。结果含分类诊断
        （category/title/advice）与能力探测（模型是否在列表、上下文窗口）。
        """
        name = str(params.get("name", "") or "")
        pc = self.cfg.providers.get(name) if name else None
        kind = str(params.get("kind", "") or "").strip() or (pc.kind if pc else "openai")
        base_url = str(params.get("base_url", "") or "").strip() or (
            (pc.base_url or "") if pc else "")
        model = str(params.get("model", "") or "").strip() or (pc.model if pc else "")
        api_key = str(params.get("api_key", "") or "").strip()
        if not api_key and pc is not None:
            api_key = resolve_api_key(name, pc) or ""
        r = await test_connection(
            kind=kind, base_url=base_url or None, api_key=api_key or None, model=model,
        )
        out = {
            "ok": r.ok,
            "latency_ms": r.latency_ms,
            "model_listed": r.model_listed,
            "context_limit": r.context_limit,
            "model": r.model,
            "error": r.error,
            "category": r.category,
            "title": r.title,
            "advice": r.advice,
        }
        # 测试结果同步进健康登记：设置页状态灯立即反映
        try:
            if name:
                if r.ok:
                    self.provider_health.record_ok(name, r.latency_ms)
                else:
                    from ...models.diagnostics import ProviderHealth
                    self.provider_health.set(ProviderHealth(
                        name=name,
                        level="down",
                        category=r.category or "unknown",
                        title=r.title or "连接失败",
                        advice=r.advice or "",
                        error=r.error[:200],
                        latency_ms=r.latency_ms,
                        checked_at=r.tested_at,
                    ))
        except Exception:  # noqa: BLE001 - 健康灯失败不影响测试结果
            pass
        return out

    async def model_probe_capabilities(self, params: dict) -> dict:
        """能力探测：对流式 / 工具调用 / 图片输入 / 思考强度各发一次最小真实调用。

        与 model_test_connection 同一套取值口径（界面改了还没保存的值优先）。
        结果不进健康登记——能力探测不评判连通性，健康灯只归「测试连接」管。
        """
        name = str(params.get("name", "") or "")
        pc = self.cfg.providers.get(name) if name else None
        kind = str(params.get("kind", "") or "").strip() or (pc.kind if pc else "openai")
        base_url = str(params.get("base_url", "") or "").strip() or (
            (pc.base_url or "") if pc else "")
        model = str(params.get("model", "") or "").strip() or (pc.model if pc else "")
        api_key = str(params.get("api_key", "") or "").strip()
        if not api_key and pc is not None:
            api_key = resolve_api_key(name, pc) or ""
        if kind not in ("openai", "anthropic"):
            # fake 等演示协议没有真实接口：三态都是「没测成」，不报错
            item = {"supported": None,
                    "note": "该服务类型没有真实接口，无法探测能力（仅 openai / anthropic 协议可探测）"}
            return {"kind": kind, "model": model, "latency_ms": 0,
                    "streaming": dict(item), "tools": dict(item),
                    "vision": dict(item), "reasoning": dict(item),
                    "summary": "该服务类型不支持能力探测。"}
        if not api_key:
            # 无 Key 的探测注定全灭，且会裸打到默认接口：提前返回可读结果
            item = {"supported": None,
                    "note": "该服务还没配 API Key，无法探测；先在上方填 Key 再试"}
            return {"kind": kind, "model": model, "latency_ms": 0,
                    "streaming": dict(item), "tools": dict(item),
                    "vision": dict(item), "reasoning": dict(item),
                    "summary": "还没配 API Key：先填 Key 并保存，再点「能力探测」。"}
        return await probe_capabilities(
            kind=kind, base_url=base_url or None, api_key=api_key or None, model=model,
        )

    def providers_health(self) -> dict:
        """全部模型服务的健康状态快照（设置页状态灯 / 运行中心用）。"""
        names = list(self.cfg.providers.keys())
        return {
            "providers": self.provider_health.snapshot(names),
            "current": self.provider_name,
            "provider_error": self.provider_error,
        }

    def _fallback_target(self) -> tuple[str, str] | None:
        """解析备用模型配置 "provider:model"；无效或指向当前服务时返回 None。"""
        raw = (self.cfg.fallback_model or "").strip()
        if not raw or ":" not in raw:
            return None
        fname, _, fmodel = raw.partition(":")
        fname, fmodel = fname.strip(), fmodel.strip()
        pc = self.cfg.providers.get(fname)
        if pc is None or pc.kind == "fake" or fname == self.provider_name:
            return None
        return (fname, fmodel or pc.model)

    async def _switch_to_fallback(self, reason: str) -> bool:
        """主模型持续失败时自动切到备用模型。成功返回 True（并发安全：
        provider_name 比对在事件循环内同步段完成，不会双切）。"""
        target = self._fallback_target()
        if target is None:
            return False
        fname, fmodel = target
        if self.provider_name == fname and self.provider_model == fmodel:
            return False
        try:
            await self.switch_model(fname, model=fmodel or None)
        except Exception as e:  # noqa: BLE001 - 备用切换失败只记录，不打断错误路径
            obs_warning("provider", f"fallback switch failed: {e}")
            return False
        obs_info("provider", f"switched to fallback {fname}:{fmodel}",
                 reason=reason[:120])
        return True
