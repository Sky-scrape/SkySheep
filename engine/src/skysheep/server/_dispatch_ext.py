"""WS handler（扩展生态域）：config(provider) / subagent / whitelist /
skills / mods / mcp / tools。自 app.py 逐字迁入，行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ._dispatch_common import _WsMethod
from .backend import ServerBackend


async def _h_config_add_provider_model(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.add_provider_model(
        str(params.get("name", "")), str(params.get("model", ""))
    )


async def _h_config_remove_provider_model(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remove_provider_model(
        str(params.get("name", "")), str(params.get("model", ""))
    )


async def _h_config_providers(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.providers_detail()


async def _h_config_save_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_provider(
        str(params.get("name", "")),
        base_url=params.get("base_url") or None,
        model=params.get("model") or None,
        api_key=params.get("api_key") or None,
        set_default=bool(params.get("set_default", False)),
        supports_reasoning=(
            bool(params["supports_reasoning"])
            if params.get("supports_reasoning") is not None else None
        ),
        supports_vision=(
            bool(params["supports_vision"])
            if params.get("supports_vision") is not None else None
        ),
        context_limit=(
            int(params["context_limit"])
            if params.get("context_limit") not in (None, "") else None
        ),
        # 温度用字符串透传：空串 = 清除该项（回到服务默认）
        temperature=(
            params["temperature"] if params.get("temperature") is not None else None
        ),
        price_in=(float(params["price_in"]) if params.get("price_in") is not None else None),
        price_out=(float(params["price_out"]) if params.get("price_out") is not None else None),
        price_cache=(float(params["price_cache"]) if params.get("price_cache") is not None else None),
        proxy=(str(params["proxy"]) if params.get("proxy") is not None else None),
    )


async def _h_config_add_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    models_param = params.get("models")
    return await backend.add_provider(
        str(params.get("name", "")),
        kind=str(params.get("kind", "openai")),
        base_url=str(params.get("base_url", "")),
        model=str(params.get("model", "")),
        api_key=params.get("api_key") or None,
        set_default=bool(params.get("set_default", False)),
        models=[str(m) for m in models_param] if isinstance(models_param, list) else None,
    )


async def _h_config_delete_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_provider(str(params.get("name", "")))


async def _h_config_restore_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.restore_provider(str(params.get("name", "")))


async def _h_config_set_provider_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_provider_enabled(
        str(params.get("name", "")), bool(params.get("enabled", True))
    )


def _gate_whitelist_enable(params: dict) -> str | None:
    enabling = bool(params.get("enabled", True))
    if enabling:
        return "启用白名单规则只能在本机界面上操作"
    return None


async def _h_subagent_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.subagents_detail()


async def _h_subagent_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    enabled = params.get("enabled")
    max_iters = params.get("max_iterations")
    max_conc = params.get("max_concurrent")
    return await backend.save_subagent_settings(
        enabled=bool(enabled) if enabled is not None else None,
        max_iterations=(int(max_iters) if max_iters not in (None, "") else None),
        max_concurrent=(int(max_conc) if max_conc not in (None, "") else None),
    )


async def _h_subagent_save_builtin(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_subagent_builtin(
        str(params.get("agent_type", "")),
        provider=str(params.get("provider", "")),
        model=str(params.get("model", "")),
        reasoning=str(params.get("reasoning", "")),
        description=str(params.get("description", "")),
        prompt=str(params.get("prompt", "")),
    )


async def _h_subagent_save_custom(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    tools = params.get("tools")
    return await backend.save_subagent_custom(
        name=str(params.get("name", "")),
        description=str(params.get("description", "")),
        prompt=str(params.get("prompt", "")),
        tools=tools if isinstance(tools, list) else str(tools if tools else "readonly"),
        provider=str(params.get("provider", "")),
        model=str(params.get("model", "")),
        enabled=bool(params.get("enabled", True)),
    )


async def _h_subagent_delete_custom(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_subagent_custom(str(params.get("name", "")))


async def _h_whitelist_remove(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 归属校验在 backend：凭枚举到的 rule_id 不能删别的项目的规则（B11）
    return await backend.remove_whitelist_rule(int(params.get("id", 0)))


async def _h_whitelist_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 添加规则 = 放行更多操作（降防护），只能在本机界面上操作；
    # 与 permission.set_mode / trust.grant 的约束一致
    return await backend.add_whitelist_rule(
        str(params.get("tool", "")),
        str(params.get("kind", "")),
        str(params.get("pattern", "")),
    )


async def _h_whitelist_clear(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 清空是收紧动作，远端也放行（与 remove 一致）
    return await backend.clear_whitelist_rules(str(params.get("kind", "")))


async def _h_whitelist_check(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.check_whitelist_rule(
        str(params.get("tool", "")), str(params.get("text", ""))
    )


async def _h_whitelist_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.export_whitelist()


async def _h_whitelist_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 启停规则：停用是收紧（远端也允许），启用是放宽 = 与 whitelist.add 同限本机
    enabling = bool(params.get("enabled", True))
    return await backend.set_whitelist_rule_enabled(
        int(params.get("id", 0)), enabling
    )


async def _h_whitelist_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 导入同样是添加规则：与本机约束一致
    rules = params.get("rules")
    return await backend.import_whitelist(rules if isinstance(rules, list) else [])


async def _h_skills_toggle(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.toggle_skill(
        str(params.get("name", "")), bool(params.get("enabled", True))
    )


async def _h_extension_audit_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    """扩展变更审计：最近 N 条（最近在前）。只读，远程也可查自己的可见部分。"""
    from ..config import skysheep_home
    from .audit import read_audit

    try:
        limit = int(params.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    return {"items": read_audit(skysheep_home(), limit=limit or 200)}


async def _h_skills_scope(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    raw = params.get("projects")
    return await backend.set_skill_scope(
        str(params.get("name", "")),
        str(params.get("mode", "all")),
        [str(p) for p in raw] if isinstance(raw, list) else None,
    )


async def _h_skills_body(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.skill_body(str(params.get("name", "")))


async def _h_skills_scan_local(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.scan_local_skills()


async def _h_skills_install(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_skill(
        str(params.get("source", "")), str(params.get("scope", "global")),
        overwrite=bool(params.get("overwrite", False)),
    )


async def _h_skills_preview_install(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.preview_skill_install(
        str(params.get("source", "")), str(params.get("scope", "global")),
        overwrite=bool(params.get("overwrite", False)),
    )


async def _h_skills_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_skill(str(params.get("name", "")))


async def _h_skills_gallery(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 纯本地只读（打包内清单 + 已装比对）：与技能清单同级，不进本机专属表
    return backend.gallery_skills()


async def _h_skills_save_from_session(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 只读生成（读会话消息 → 返回草稿文本）：写盘发生在 skills.save_draft
    return await backend.save_skill_from_session(str(params.get("id", "")))


async def _h_skills_save_draft(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 落盘写 SKILL.md：与 skills.install 同级（技能正文进 system prompt），仅本机
    return await backend.save_skill_draft(
        str(params.get("name", "")),
        str(params.get("content", "")),
        scope=str(params.get("scope", "global")),
    )


# ---- Mods 扩展（实验性）----
# 只读方法（list/get/get_template/official）各端可看；全部写方法（安装/启停/删除/
# 草稿/测试器）与本机执行面同级——Mod 是第三方代码且能收紧权限门行为，绝不能被
# 远程持令牌客户端安装或改动。

async def _h_mods_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.list_mods()


async def _h_mods_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.get_mod(str(params.get("id", "")))


async def _h_mods_get_template(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.mod_template()


async def _h_mods_official(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.official_mods()


async def _h_mods_install(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_mod(str(params.get("source", "")))


async def _h_mods_confirm_install(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.confirm_install_mod(str(params.get("install_token", "")))


async def _h_mods_install_official(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_official_mod(str(params.get("id", "")))


async def _h_mods_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_mod(str(params.get("id", "")))


async def _h_mods_toggle(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.toggle_mod(
        str(params.get("id", "")), bool(params.get("enabled", True))
    )


async def _h_mods_set_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_mods_enabled(bool(params.get("enabled", False)))


async def _h_mods_claude_action(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    """Claude 兼容模式的 UI 交互回传（按钮点击 / 输入变更）。

    P4：只做记录与 ack；真正把 action 投递给 Mod handler（tool.check 之外的
    交互事件）留给 P5（需要额外的事件名与 handler 契约）。
    """
    return await backend.dispatch_mod_action(
        mod_id=str(params.get("mod_id", "")),
        action_id=str(params.get("action_id", "")),
        kind=str(params.get("kind", "")),
        value=params.get("value"),
        session_id=str(params.get("session_id", "")),
    )


async def _h_mods_save_draft(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 草稿写进 <项目>/.skysheep/mods-drafts/：安装仍要两段人工确认
    return await backend.save_mod_draft(params)


async def _h_mods_test(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 实跑一个 handler（不记执行记录）：本机专属，与 hooks.test 同姿态
    return await backend.test_mod(params)


async def _h_mcp_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.import_mcp_servers(
        snippet=str(params.get("snippet", "")),
        path=str(params.get("path", "")),
        scope=str(params.get("scope", "global")),
        overwrite=bool(params.get("overwrite", False)),
        # M10：含 stdio 定义时先回 needs_confirm，前端确认后带 true 重试
        confirmed=bool(params.get("confirmed", False)),
    )


async def _h_mcp_save_server(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_mcp_server(
        str(params.get("name", "")),
        command=str(params.get("command", "")),
        args=params.get("args") or None,
        url=str(params.get("url", "")),
        env=params.get("env") or None,
        headers=params.get("headers") or None,
        readonly=bool(params.get("readonly", False)),
        timeout=float(params.get("timeout") or 0),
        scope=str(params.get("scope", "global")),
    )


async def _h_mcp_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_mcp_server(
        str(params.get("name", "")), str(params.get("scope", "global"))
    )


async def _h_mcp_add_preset(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.add_mcp_preset(
        str(params.get("name", "")), str(params.get("scope", "global"))
    )


async def _h_mcp_set_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_mcp_enabled(
        str(params.get("name", "")),
        bool(params.get("enabled", True)),
        str(params.get("scope", "global")),
    )


async def _h_mcp_reconnect(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    warnings = await backend._reconnect_mcp()
    return {"mcp": backend._mcp_status_list(backend.mcp), "mcp_warnings": warnings}


async def _h_mcp_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"mcp": backend._mcp_status_list(backend.mcp)}


async def _h_tools_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"tools": [
        {"name": t.name, "safety": t.safety.value, "description": t.description}
        for t in backend.agent.registry.all()
    ]}


async def _h_whitelist_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 走 backend：除库字段外还给每条规则带 stale / stale_reason（失效徽章）
    return {"rules": await backend.list_whitelist_rules()}


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "config.add_provider_model": _WsMethod(_h_config_add_provider_model, local_only=True),
    "config.remove_provider_model": _WsMethod(_h_config_remove_provider_model, local_only=True),
    "config.providers": _WsMethod(_h_config_providers),
    "config.save_provider": _WsMethod(_h_config_save_provider, local_only=True),
    "config.add_provider": _WsMethod(_h_config_add_provider, local_only=True),
    "config.delete_provider": _WsMethod(_h_config_delete_provider, local_only=True),
    "config.restore_provider": _WsMethod(_h_config_restore_provider, local_only=True),
    "config.set_provider_enabled": _WsMethod(_h_config_set_provider_enabled, local_only=True),
    "subagent.get": _WsMethod(_h_subagent_get),
    "subagent.save": _WsMethod(_h_subagent_save, local_only=True),
    "subagent.save_builtin": _WsMethod(_h_subagent_save_builtin, local_only=True),
    "subagent.save_custom": _WsMethod(_h_subagent_save_custom, local_only=True),
    "subagent.delete_custom": _WsMethod(_h_subagent_delete_custom, local_only=True),
    "whitelist.remove": _WsMethod(_h_whitelist_remove),
    "whitelist.add": _WsMethod(
        _h_whitelist_add, local_only=True,
        local_error="白名单添加只能在本机界面上操作",
    ),
    "whitelist.clear": _WsMethod(_h_whitelist_clear),
    "whitelist.check": _WsMethod(_h_whitelist_check),
    "whitelist.export": _WsMethod(_h_whitelist_export),
    "whitelist.enable": _WsMethod(_h_whitelist_enable, local_gate=_gate_whitelist_enable),
    "whitelist.import": _WsMethod(
        _h_whitelist_import, local_only=True,
        local_error="白名单导入只能在本机界面上操作",
    ),
    "skills.toggle": _WsMethod(_h_skills_toggle, local_only=True),
    "extension.audit.list": _WsMethod(_h_extension_audit_list),
    "skills.scope": _WsMethod(_h_skills_scope, local_only=True),
    "skills.body": _WsMethod(_h_skills_body),
    "skills.scan_local": _WsMethod(_h_skills_scan_local),
    "skills.install": _WsMethod(_h_skills_install, local_only=True),
    "skills.preview_install": _WsMethod(_h_skills_preview_install, local_only=True),
    "skills.delete": _WsMethod(_h_skills_delete, local_only=True),
    "skills.gallery": _WsMethod(_h_skills_gallery),
    "skills.save_from_session": _WsMethod(_h_skills_save_from_session),
    "skills.save_draft": _WsMethod(_h_skills_save_draft, local_only=True),
    # Mods 扩展（实验性）：写方法全部仅本机（第三方代码 + 收紧权限门的能力面）
    "mods.list": _WsMethod(_h_mods_list),
    "mods.get": _WsMethod(_h_mods_get),
    "mods.get_template": _WsMethod(_h_mods_get_template),
    "mods.official": _WsMethod(_h_mods_official),
    "mods.install": _WsMethod(_h_mods_install, local_only=True),
    "mods.confirm_install": _WsMethod(_h_mods_confirm_install, local_only=True),
    "mods.install_official": _WsMethod(_h_mods_install_official, local_only=True),
    "mods.delete": _WsMethod(_h_mods_delete, local_only=True),
    "mods.toggle": _WsMethod(_h_mods_toggle, local_only=True),
    "mods.set_enabled": _WsMethod(_h_mods_set_enabled, local_only=True),
    "mods_claude.action": _WsMethod(_h_mods_claude_action, local_only=True),
    "mods.save_draft": _WsMethod(_h_mods_save_draft, local_only=True),
    "mods.test": _WsMethod(
        _h_mods_test, local_only=True, local_error="Mod 测试器只能在本机界面上操作",
    ),
    "mcp.import": _WsMethod(_h_mcp_import, local_only=True),
    "mcp.save_server": _WsMethod(_h_mcp_save_server, local_only=True),
    "mcp.delete": _WsMethod(_h_mcp_delete, local_only=True),
    "mcp.add_preset": _WsMethod(_h_mcp_add_preset, local_only=True),
    "mcp.set_enabled": _WsMethod(_h_mcp_set_enabled, local_only=True),
    "mcp.reconnect": _WsMethod(_h_mcp_reconnect, local_only=True),
    "mcp.status": _WsMethod(_h_mcp_status),
    "tools.list": _WsMethod(_h_tools_list),
    "whitelist.list": _WsMethod(_h_whitelist_list),
}
