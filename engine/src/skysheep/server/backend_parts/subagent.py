"""子代理设置与定义管理（独立设置页）。

D6 基础设施与迁移域（backend.py Mixin 拆分批 2 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

字段所有权（D6，设计 §3.2）：`subagent_store`（SubagentStore 实例，主类
__init__ 一次铺齐）——内置覆盖 / 自定义定义的读写全在本域；设置保存走
config 的 set_subagent_settings_in_config + 任务簿热更新（内核 TaskManager）。
`_subagent_provider` / `_subagent_registry` 被内核 runtime 池（_get_runtime）
与 _bind_project 以方法引用取用，MRO 语义不变。

跨域只读（不收敛，注记）：`self._apply_registry_to_agents()`（内核装配）、
`self._base_agent.registry`（内核基底 Agent）、`self.cfg` / `self.provider` /
`self.provider_name`（内核模型面）——经共享 self 取用。
"""

from __future__ import annotations

from ...config import (
    PRESETS,
    REASONING_EFFORT_LABELS,
    REASONING_EFFORTS,
    ConfigError,
    ProviderConfig,
    load_config,
    resolve_api_key,
    set_subagent_settings_in_config,
)
from ...core.subagent import BUILTIN_ROLE_PROMPTS
from ...core.subagent_store import (
    BUILTIN_AGENT_TYPES,
    BUILTIN_DESCRIPTIONS,
    BUILTIN_DISPLAY,
    SubagentDef,
    SubagentDefError,
    validate_subagent_name,
)
from ...models.factory import build_provider
from ...tools import COMPUTER_TOOL_NAMES, Safety, ToolRegistry


class SubagentMixin:
    """子代理设置与定义管理（subagent 相关 WS 方法的实现面）。方法自 backend.py 逐字搬入。"""

    # 子代理设置：读当前值 / 保存并热生效

    def subagent_settings(self) -> dict:
        return {
            "enabled": self.cfg.subagent_enabled,
            "max_iterations": self.cfg.subagent_max_iterations,
            "max_concurrent": self.cfg.subagent_max_concurrent,
            "main_max_iterations": self.cfg.max_iterations,
        }

    async def save_subagent_settings(
        self, *, enabled: bool | None = None, max_iterations: int | None = None,
        max_concurrent: int | None = None,
    ) -> dict:
        """保存子代理设置并立即生效：开关控注册表、轮数与并发热更新任务簿，不用重启。"""
        if max_iterations is not None:
            try:
                max_iterations = int(max_iterations)
            except (TypeError, ValueError):
                raise RuntimeError("迭代轮数要是 1–100 之间的整数") from None
            if not 1 <= max_iterations <= 100:
                raise RuntimeError("迭代轮数要在 1–100 之间")
        if max_concurrent is not None:
            try:
                max_concurrent = int(max_concurrent)
            except (TypeError, ValueError):
                raise RuntimeError("并发上限要是 1–8 之间的整数") from None
            if not 1 <= max_concurrent <= 8:
                raise RuntimeError("并发上限要在 1–8 之间")
        try:
            set_subagent_settings_in_config(
                enabled=enabled, max_iterations=max_iterations,
                max_concurrent=max_concurrent,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        self.tasks.set_max_iterations(self.cfg.subagent_max_iterations)
        self.tasks.set_max_concurrent(self.cfg.subagent_max_concurrent)
        if enabled is not None:
            self._apply_registry_to_agents()
        return self.subagent_settings()

    # ---- 子代理：定义管理（独立设置页） ----

    def _subagent_provider(self, provider_name: str, model: str, reasoning: str):
        """按子代理定义解析出 Provider。

        provider 留空 = 跟随主对话当前模型（用临时实例，不污染主 provider 的
        思考强度状态）；指定了服务就按 (服务, 模型, 思考强度) 现场构建。
        """
        provider_name = (provider_name or "").strip()
        model = (model or "").strip()
        reasoning = (reasoning or "").strip().lower()
        if provider_name and provider_name in self.cfg.providers:
            data = self.cfg.providers[provider_name].model_dump()
            if model:
                data["model"] = model
            if reasoning:
                data["reasoning_effort"] = reasoning
            return build_provider(provider_name, ProviderConfig(**data), self.cfg.network)
        if self.provider is None:
            raise RuntimeError("主对话当前没有可用模型，子代理无法启动——先到模型服务里配好 Key")
        if reasoning and self.provider_name and self.provider_name in self.cfg.providers:
            pc = self.cfg.providers[self.provider_name].model_copy(
                update={"reasoning_effort": reasoning}
            )
            return build_provider(self.provider_name, pc, self.cfg.network)
        return self.provider

    def _subagent_registry(self, policy):
        """把工具范围策略解析成子代理注册表。

        派生工具（spawn_agent/check_task/wait_task）永远不进子代理——禁止递归派生；
        就算给了写工具，SubagentGate 也会自动拒绝需要确认的操作。
        电脑控制七件套不随「全部工具 / 仅只读」顺手进子代理（截屏/窗口列表属于
        隐私敏感只读）；勾选模式是用户显式点名，不受此限。
        """
        base = self._base_agent.registry
        exclude = {"spawn_agent", "check_task", "wait_task"}
        if isinstance(policy, str):
            policy = (policy or "").strip().lower() or "readonly"
            exclude |= set(COMPUTER_TOOL_NAMES)  # 桶装策略：电脑控制一律不带
            if policy == "all":
                tools = [t for t in base.all() if t.name not in exclude]
            else:
                tools = [
                    t for t in base.all()
                    if t.safety == Safety.READONLY and t.name not in exclude
                ]
        else:
            want = {str(n).strip() for n in (policy or []) if str(n).strip()}
            tools = [t for t in base.all() if t.name in want and t.name not in exclude]
        if not tools:
            raise RuntimeError("子代理工具集为空：至少要有一个可用工具")
        return ToolRegistry(tools)

    def subagents_detail(self) -> dict:
        """「子代理」设置页的一次性数据：全局设置 + 定义 + 可选项。"""
        providers = []
        for name, pc in self.cfg.providers.items():
            models = list(pc.models) or ([pc.model] if pc.model else [])
            providers.append({
                "name": name,
                "model": pc.model,
                "models": models,
                "has_key": resolve_api_key(name, pc) is not None,
                # 自定义模型弹层用：显示名（预设如「智谱」）与内置/自定义分组，
                # 与 config.providers 的 providers_detail 同口径
                "label": pc.label or name,
                "is_preset": name in PRESETS,
            })
        tools = [
            {"name": t.name, "safety": t.safety.value, "description": t.description}
            for t in self._base_agent.registry.all()
            if t.name not in ("spawn_agent", "check_task", "wait_task")
        ]
        return {
            **self.subagent_settings(),
            "builtin": {t: ov.model_dump() for t, ov in self.subagent_store.builtin.items()},
            "builtin_display": dict(BUILTIN_DISPLAY),
            # 生效描述（覆盖值优先，否则内置默认）+ 默认角色提示词（编辑框占位展示）
            "builtin_desc": {
                t: (
                    ov.description.strip()
                    or BUILTIN_DESCRIPTIONS.get(t, "")
                )
                for t, ov in self.subagent_store.builtin.items()
            },
            "builtin_default_prompt": dict(BUILTIN_ROLE_PROMPTS),
            "custom": [d.model_dump() for d in self.subagent_store.custom],
            "providers": providers,
            "tools": tools,
            "reasoning_efforts": [
                {"value": v, "label": REASONING_EFFORT_LABELS.get(v, v)}
                for v in REASONING_EFFORTS
            ],
        }

    async def save_subagent_builtin(
        self, agent_type: str, *, provider: str = "", model: str = "", reasoning: str = "",
        description: str = "", prompt: str = "",
    ) -> dict:
        """内置子代理的定制：模型/思考强度覆盖 + 说明与角色提示词的改写。

        description/prompt 留空 = 恢复该内置型的内置默认（不是删掉功能）。"""
        if provider and provider not in self.cfg.providers:
            raise RuntimeError("未知的模型服务: " + provider)
        if agent_type not in BUILTIN_AGENT_TYPES:
            raise RuntimeError("未知的内置子代理: " + str(agent_type))
        try:
            ov = self.subagent_store.set_override(
                agent_type, provider=provider, model=model, reasoning=reasoning,
                description=description, prompt=prompt,
            )
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        self._apply_registry_to_agents()
        return {"agent_type": agent_type, **ov.model_dump()}

    async def save_subagent_custom(
        self,
        *,
        name: str,
        description: str = "",
        prompt: str = "",
        tools="readonly",
        provider: str = "",
        model: str = "",
        reasoning: str = "",
        enabled: bool = True,
    ) -> dict:
        """新建或更新一个自定义子代理（同名即更新），保存后立即可用。"""
        try:
            name = validate_subagent_name(name)
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        description = (description or "").strip()[:500]
        prompt = (prompt or "").strip()[:20_000]
        reasoning = (reasoning or "").strip().lower()
        if reasoning and reasoning not in REASONING_EFFORTS:
            raise RuntimeError("思考强度只支持 留空 / " + " / ".join(REASONING_EFFORTS))
        if provider and provider not in self.cfg.providers:
            raise RuntimeError("未知的模型服务: " + provider)
        known = {
            t.name for t in self._base_agent.registry.all()
        } - {"spawn_agent", "check_task", "wait_task"}
        if isinstance(tools, str):
            tools = tools.strip().lower() or "readonly"
            if tools not in ("all", "readonly"):
                raise RuntimeError("工具范围只能是 全部工具 / 仅只读 / 勾选工具列表")
        else:
            want = [str(n).strip() for n in (tools or []) if str(n).strip()]
            if not want:
                raise RuntimeError("自定义工具范围至少要勾选一个工具")
            unknown = sorted(set(want) - known)
            if unknown:
                raise RuntimeError("未知工具: " + "、".join(unknown))
            tools = want
        d = SubagentDef(
            name=name,
            description=description,
            prompt=prompt,
            tools=tools,
            provider=provider.strip(),
            model=model.strip(),
            reasoning=reasoning,
            enabled=bool(enabled),
        )
        self.subagent_store.upsert_custom(d)
        self._apply_registry_to_agents()  # 刷新 spawn_agent 的说明文字
        return d.model_dump()

    async def delete_subagent_custom(self, name: str) -> dict:
        try:
            self.subagent_store.remove_custom((name or "").strip())
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        self._apply_registry_to_agents()
        return {"removed": name}
