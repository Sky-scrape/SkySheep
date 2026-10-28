"""WS handler（轮次与会话运行域）：boot / chat / permission / stop /
tasks / aux / model / runs / probe。自 app.py 逐字迁入，行为零变化。

# 本文件为机器搬移产物（P28 拆分）：正文与 app.py 原文逐字一致，
# 改行为请同步 test_dispatch_gates / test_local_only_methods 的锚定断言。
"""

from __future__ import annotations

from ..config import resolve_api_key
from ._dispatch_common import _remote_scope, _WsMethod
from .backend import ServerBackend


def _gate_permission_set_mode(params: dict) -> str | None:
    mode = str(params.get("mode", "confirm"))
    # 「自动编辑 / 完全访问」会放宽写入与执行的确认：只能在本机界面上切换，
    # 不允许被局域网客户端（或远程驱动的前端）打开
    if mode in ("accept_edits", "full_access"):
        return "放宽权限的档位（自动编辑/完全访问）只能在本机界面上切换"
    return None


async def _h_boot(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 安全审查 M7：远程客户端拿到的快照不含本机绝对路径与服务 endpoint
    return await backend.snapshot(local=local)


async def _h_chat_send(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    text = str(params.get("text", "")).strip()
    images = params.get("images")
    regenerate = bool(params.get("regenerate", False))
    if not text and not (isinstance(images, list) and images) and not regenerate:
        raise RuntimeError("empty text")
    members = params.get("members")
    raw_refs = params.get("refs")
    # 圆桌本轮覆盖值：只在客户端显式传了才覆盖 config（None=用配置值）
    raw_debate = params.get("debate_rounds")
    raw_chair = params.get("chair_answers")
    # AI 总管（二期，建队轮生效）：director_mode 只认 "ai"/"user"（其余按缺省
    # 用户总管处理，与 chat.send 其他错型参数「coercion 吞掉不炸」同一姿态）；
    # director 为 {provider, model}，provider 为空视同未指定
    director_mode = "ai" if params.get("director_mode") == "ai" else "user"
    raw_director = params.get("director")
    director = None
    if isinstance(raw_director, dict):
        d_provider = str(raw_director.get("provider", "") or "").strip()
        if d_provider:
            director = {
                "provider": d_provider,
                "model": str(raw_director.get("model", "") or "").strip(),
            }
    # 建队来源模板名（前端「从模板创建」随建队轮带上）：只在建队路径消费，
    # 记进编排器快照供常驻登记绑定回模板；错型/缺省按空串吞掉不炸
    team_template = str(params.get("team_template", "") or "").strip()
    return await backend.send(
        text,
        emit,
        plan_mode=bool(params.get("plan_mode", False)),
        roundtable=bool(params.get("roundtable", False)),
        members=members if isinstance(members, list) else None,
        images=images if isinstance(images, list) else None,
        session_id=str(params["session_id"]) if params.get("session_id") else None,
        wants_title=bool(params.get("wants_title", False)),
        regenerate=bool(params.get("regenerate", False)),
        compare=bool(params.get("compare", False)),
        refs=[str(r) for r in raw_refs] if isinstance(raw_refs, list) else None,
        debate_rounds=(
            max(0, min(2, int(raw_debate)))
            if isinstance(raw_debate, int) and not isinstance(raw_debate, bool)
            else None
        ),
        chair_answers=raw_chair if isinstance(raw_chair, bool) else None,
        team=bool(params.get("team", False)),
        adversarial=bool(params.get("adversarial", False)),
        director_mode=director_mode,
        director=director,
        team_template=team_template,
        local=local,
    )


async def _h_chat_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # session_id 可选：给出时查那个会话的 runtime（分屏列的上下文徽标辅路），
    # 查无/已淘汰由后端显式报错，不静默回退当前会话。
    # 远程来源不开放按任意 sid 查询：runtime 状态含 todo 全文与上下文明细，
    # 收敛到该客户端连接正在交互的会话（与 tasks.list 的远端口径同源，B 族）。
    sid = str(params.get("session_id", "") or "") if isinstance(params, dict) else ""
    only = _remote_scope(local)
    if only is not None:
        if not only:
            raise RuntimeError("尚未绑定会话：请先打开一个会话再查询状态")
        sid = only
    return await backend.status(session_id=sid or None)


async def _h_chat_compact(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.compact_now()


async def _h_chat_aux(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    text = str(params.get("text", ""))
    aux_id = str(params.get("aux_id", "aux1"))
    # 远端调用走一次性历史：不读本机共享面板历史、也不写入（审查 P1-3 收口）
    return await backend.chat_aux(text, aux_id, emit, local=local)


async def _h_permission_respond(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {
        "delivered": backend.respond_permission(
            str(params.get("request_id", "")), str(params.get("decision", "deny"))
        )
    }


async def _h_permission_mode(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"mode": backend.permission_mode()}


async def _h_permission_set_mode(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    mode = str(params.get("mode", "confirm"))
    return await backend.set_permission_mode(mode)


async def _h_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    sid = str(params["session_id"]) if params.get("session_id") else ""
    # 远程连接只能停止自己正在交互的会话；连接尚未绑定会话时不能把空 sid
    # 解释成后端当前活动会话，否则拿到令牌的客户端可停止本机其他会话。
    if not local and not sid:
        return {"cancelled": False}
    return {"cancelled": backend.cancel_run(sid or None)}


async def _h_tasks_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 远程客户端只能看自己正在交互的会话的子代理任务（安全审查 B13：
    # 任务详情含 prompt/result，本机任务簿保持全局视图不变）。作用域取
    # 连接级会话，不读全局 backend.session；未绑定时 fail closed 返回空。
    only = _remote_scope(local)
    if only == "":
        return {"tasks": []}
    return await backend.tasks_list(params, session_id=only)


async def _h_tasks_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    only = _remote_scope(local)
    if only == "":
        raise RuntimeError("尚未绑定会话：请先打开一个会话再查看任务")
    return await backend.tasks_get(params, session_id=only)


async def _h_tasks_cancel_all(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    only = _remote_scope(local)
    if only == "":
        return {"cancelled": False}
    return await backend.tasks_cancel_all(session_id=only)


async def _h_tasks_cancel(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    only = _remote_scope(local)
    if only == "":
        return {"cancelled": False}
    return await backend.tasks_cancel(params, session_id=only)


async def _h_aux_model_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.aux_model_state()


async def _h_aux_model_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_aux_model(params)


async def _h_aux_clear(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    aux_id = str(params.get("aux_id", "aux1"))
    return backend.aux_clear(aux_id)


async def _h_model_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {
        "current": backend.provider_name,
        "providers": {
            name: {
                "kind": pc.kind,
                "model": pc.model,
                "has_key": resolve_api_key(name, pc) is not None,
            }
            for name, pc in backend.cfg.providers.items()
        },
    }


async def _h_model_reasoning(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {
        "reasoning": backend.reasoning_state(),
    }


async def _h_model_set_reasoning(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_reasoning_effort(
        str(params.get("effort", "auto")),
        str(params["provider"]) if params.get("provider") else None,
    )


async def _h_model_switch(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.switch_model(
        str(params.get("name", "")), model=params.get("model") or None
    )


async def _h_default_model_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.default_model_state()


async def _h_default_model_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_default_model(params)


async def _h_runs_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 远程客户端只能看自己正在交互的会话的运行（运行条目含原文首段与错误摘要，
    # 与 tasks.list 同一隔离口径 B13）。作用域取连接级会话（_remote_scope），
    # 不读全局 backend.session——后者会被别的连接/本机窗口切走。
    only = _remote_scope(local)
    if only == "":
        return {"runs": []}  # 远程尚未绑定会话：fail closed，不回退到全局活动会话
    include_terminal = bool(params.get("include_terminal", True))
    limit = min(200, max(1, int(params.get("limit", 100))))
    # 只查一次：旧实现把 list_runs 在布尔与三元里各调一遍，非空列表时会
    # 重复 I/O，两次快照还可能不一致。
    return {"runs": backend.runs.list_runs(
        session_id=only or None, include_terminal=include_terminal, limit=limit,
    )}


async def _h_runs_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    run = backend.runs.get(str(params.get("run_id", "") or ""))
    if run is None:
        raise RuntimeError("unknown run_id: " + str(params.get("run_id", "")))
    only = _remote_scope(local)
    if only is not None and (not only or run.session_id != only):
        raise RuntimeError("该运行不属于当前会话")
    return {"run": run.to_dict()}


async def _h_runs_cancel(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    run_id = str(params.get("run_id", "") or "")
    run = backend.runs.get(run_id)
    if run is None:
        raise RuntimeError("unknown run_id: " + run_id)
    only = _remote_scope(local)
    if only is not None and (not only or run.session_id != only):
        raise RuntimeError("该运行不属于当前会话")
    # 主轮运行：真取消 = 停掉该会话正在跑的 task；子代理：转任务簿取消
    cancelled = False
    if run.kind == "turn" and run.state in ("queued", "thinking", "tool_running",
                                            "waiting_permission", "retrying"):
        cancelled = backend.cancel_run(run.session_id or None)
    elif run.kind == "subagent":
        tid = run_id.removeprefix("subagent-")
        if backend.tasks is not None:
            try:
                cancelled = backend.tasks.cancel_task(tid, session_id=run.session_id or None)
            except Exception:  # noqa: BLE001
                cancelled = False
    if cancelled:
        backend.runs.cancel(run_id)
    return {"cancelled": bool(cancelled), "run_id": run_id}


async def _h_runs_retry(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    """失败/中断运行的一键重发/重跑：把原文再发一次（同会话）。

    语义是「从这里继续」而不是快照回放：引擎不保存完整运行现场，重发等价于
    用户把同一句话再发一遍——对中断的主轮与失败的子代理都是诚实可行的恢复
    方式。子代理运行则按原 agent_type / prompt / isolated 真正重新派生一个
    同型任务（不是把 prompt 当普通消息重发）；主轮仍回传原文交给前端走
    chat.send。
    """
    run_id = str(params.get("run_id", "") or "")
    run = backend.runs.retryable_run(run_id)
    if run is None:
        raise RuntimeError("该运行不可重发（只有失败/中断且带原文的运行支持）")
    only = _remote_scope(local)
    if only is not None and (not only or run.session_id != only):
        raise RuntimeError("该运行不属于当前会话")
    if run.kind == "subagent":
        # 真正重跑子代理：按原 agent_type + prompt（+ 隔离标志）重新派生。
        # 任务簿可能已把旧记录裁剪（KEEP 50），run 记录里保留了最小重跑信息。
        if backend.tasks is None:
            raise RuntimeError("子代理未启用，无法重跑")
        agent_type = run.agent_type or "task"
        prompt = run.retry_text or ""
        if not prompt:
            raise RuntimeError("该子代理运行没有可重跑的原文")
        try:
            new_id = backend.tasks.start_background(
                agent_type, prompt, session_id=run.session_id or "",
                isolated=bool(run.isolated),
            )
        except Exception as e:
            raise RuntimeError(f"重跑子代理失败：{e}") from e
        return {"ok": True, "kind": "subagent", "run_id": run_id,
                "task_id": new_id, "session_id": run.session_id}
    # 复用 runs.cancel 已有语义（turn 的重发交给前端走 chat.send；这里只回传参数）
    return {"ok": True, "kind": "turn", "run_id": run_id, "session_id": run.session_id,
            "text": run.retry_text}


async def _h_model_test(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.model_test_connection(params)


async def _h_model_probe_capabilities(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.model_probe_capabilities(params)


async def _h_model_providers_health(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.providers_health()


async def _h_permission_queue(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    """当前未决权限请求队列（本机跨会话聚合，运行中心「等待确认」分区用）。

    返回与 permission_request 事件同构的字段（request_id / session_id / tool_name /
    detail / safety / origin / scope / reversible / revert_note / diff / note /
    rule_kind / rule_pattern），前端断线重连或刷新后可据此重建确认卡——
    后端那一轮还挂在等决策上，卡片丢了等于「假死」（安全审查 M12 同源问题）。
    决策仍走 permission.respond。

    远程连接只能看到自己绑定会话的未决请求：队列里含工具名、影响面与文件
    diff，跨会话聚合会泄露其他会话在做什么（B 族）。
    """

    def _drain(pending_map: dict, sid: str = "") -> list[dict]:
        out: list[dict] = []
        for rid, pending in pending_map.items():
            rule = getattr(pending, "always_rule", None)
            out.append({
                "request_id": rid,
                "session_id": sid,
                "tool_name": getattr(pending, "tool_name", "") or "",
                "detail": (getattr(pending, "detail", "") or "")[:200],
                "safety": getattr(getattr(pending, "safety", None), "value", ""),
                "origin": getattr(pending, "origin", "") or "",
                "scope": getattr(pending, "scope", "") or "",
                "reversible": bool(getattr(pending, "reversible", False)),
                "revert_note": getattr(pending, "revert_note", "") or "",
                "diff": getattr(pending, "diff", "") or "",
                "note": getattr(pending, "note", "") or "",
                "rule_kind": getattr(rule, "kind", "") or "",
                "rule_pattern": getattr(rule, "pattern", "") or "",
            })
        return out

    only = _remote_scope(local)
    if only == "":
        return {"pending": []}  # 远程尚未绑定会话：不回退到全局跨会话聚合
    out: list[dict] = []
    for rt in backend.runtimes.values():
        if only is not None and rt.sid != only:
            continue
        out.extend(_drain(getattr(rt.agent, "_pending", {}) or {}, rt.sid))
    base = getattr(backend, "_base_agent", None)
    if base is not None and only is None:
        # 基础 agent 不属于具体会话：只向本机公开（远程需要明确的会话归属）
        out.extend(_drain(getattr(base, "_pending", {}) or {}))
    return {"pending": out}


async def _h_config_probe_models(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.probe_models(
        name=str(params.get("name", "")),
        kind=str(params.get("kind", "")),
        base_url=str(params.get("base_url", "")),
        api_key=str(params.get("api_key", "")),
    )


async def _h_config_probe_context(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.probe_context(
        name=str(params.get("name", "")),
        kind=str(params.get("kind", "")),
        base_url=str(params.get("base_url", "")),
        api_key=str(params.get("api_key", "")),
        model=str(params.get("model", "")),
    )


# ---- 本域方法的注册表条目（自 app.py 的 _WS_METHODS 逐字切片） ----
METHODS: dict[str, _WsMethod] = {
    "boot": _WsMethod(_h_boot),
    "chat.send": _WsMethod(_h_chat_send),
    "chat.status": _WsMethod(_h_chat_status),
    # 上下文压缩取的是桌面全局活动会话的 agent（backend.compact_now 用 self.agent）：
    # 远程连接压不到自己绑定的会话，反而会压到本机正在使用的另一个会话
    # （安全审查 L05，与 tasks.list 的 B 族口径同源）——收敛为本机专属
    "chat.compact": _WsMethod(
        _h_chat_compact, local_only=True,
        local_error="压缩上下文只能在桌面端本机执行",
    ),
    "chat.aux": _WsMethod(_h_chat_aux),
    "permission.respond": _WsMethod(_h_permission_respond),
    "permission.mode": _WsMethod(_h_permission_mode),
    "permission.set_mode": _WsMethod(_h_permission_set_mode, local_gate=_gate_permission_set_mode),
    "stop": _WsMethod(_h_stop),
    "tasks.list": _WsMethod(_h_tasks_list),
    "tasks.get": _WsMethod(_h_tasks_get),
    "tasks.cancel_all": _WsMethod(_h_tasks_cancel_all),
    "tasks.cancel": _WsMethod(_h_tasks_cancel),
    "aux.model.get": _WsMethod(_h_aux_model_get),
    "aux.model.set": _WsMethod(_h_aux_model_set, local_only=True),
    "aux.clear": _WsMethod(_h_aux_clear),
    "model.list": _WsMethod(_h_model_list),
    "model.reasoning": _WsMethod(_h_model_reasoning),
    # 思考强度写 config.toml 的 provider 配置并对全部共享 provider 的会话热生效
    # （set_reasoning_effort 落盘 + 重建 provider）：服务配置/费用行为面，与
    # model.switch、session.reasoning_set（均本机专属）同一口径
    "model.set_reasoning": _WsMethod(_h_model_set_reasoning, local_only=True),
    "model.switch": _WsMethod(_h_model_switch, local_only=True),
    "default_model.get": _WsMethod(_h_default_model_get),
    "default_model.set": _WsMethod(_h_default_model_set, local_only=True),
    "runs.list": _WsMethod(_h_runs_list),
    "runs.get": _WsMethod(_h_runs_get),
    "runs.cancel": _WsMethod(_h_runs_cancel),
    "runs.retry": _WsMethod(_h_runs_retry),
    "model.test": _WsMethod(_h_model_test, local_only=True),
    "model.probe_capabilities": _WsMethod(_h_model_probe_capabilities, local_only=True),
    "model.providers_health": _WsMethod(_h_model_providers_health),
    "permission.queue": _WsMethod(_h_permission_queue),
    "config.probe_models": _WsMethod(_h_config_probe_models, local_only=True),
    "config.probe_context": _WsMethod(_h_config_probe_context, local_only=True),
}
