"""会话模型与思考强度、分叉/恢复、模型路由与会话观测。

D1 会话与运行时域（backend.py Mixin 拆分批 5b 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变（方法内无相对导入，故无导入
深度改写；唯一改写在本文件之外——`ServerBackend` 静态自引用只出现在
project_mgmt.py，见该文件头）。

五段相邻职责：
- 会话级模型：分屏独立选模型（session.model_switch / session.model.get）与
  全局切换（model.switch），以及「新会话默认模型」「辅助对话模型」两套偏好；
- 会话级思考强度：session.reasoning_get / session.reasoning_set 与全局
  set_reasoning_effort；
- 模型三段解析（会话覆盖 → 默认模型快照 → 全局）与 provider 生命周期
  （_runtime_provider / _apply_model_routing / _release_turn_providers /
  _close_provider_after）：runtime 首建、LRU 淘汰重建、热切模型都走这里；
- 会话分叉 / 恢复（fork_session / resume_session）；
- 会话观测：/compact（compact_now）、/status（status）与上下文明细
  （_context_detail / _estimate_session_context）。

内核共用件（不进本文件，由 backend.py 在自身定义完成后注入本模块命名空间，
见 backend.py 末尾的接线注记）：`HISTORY_SYNC_LIMIT`（前端历史窗口口径）、
`_msg_brief` / `_window_briefs`（历史 brief 的唯一组装处，
tests/test_message_brief_contract.py 锚定）。backend_parts 各模块不得 import
backend（会成环，见 backend_parts/__init__.py 的文件头约定）。

字段所有权（D1，本批只注记不搬）：`_session_models`（会话级模型覆盖）、
`_default_model_sessions`（创建时刻的默认模型快照）、`_session_efforts`
（会话级思考档位）由主类 __init__ 一次铺齐。三者在本文件读写，但删除会话
（含清空空会话）时的清理面在 LifecycleMixin（backend_parts/lifecycle.py），
截断（truncate_session）只截库、重载 runtime 历史，不清理这三项——清理义务
写在两域文件头（互指），改动需双方会签。本 Mixin 无 __init__、无类级可变
属性。

peek_session_messages / session_messages_earlier 不在本文件：设计 §2.1 D1
外迁清单未列（会话只读历史窗口，随内核留 backend.py）。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from ... import __version__
from ...bgtasks import spawn_bg
from ...config import (
    REASONING_EFFORT_LABELS,
    REASONING_EFFORTS,
    load_config,
    update_provider_in_config,
)
from ...core import Agent, build_system_prompt
from ...core.context import compact_history, estimate_text_tokens, estimate_tokens
from ...core.effort import estimate_effort
from ...core.prompt import (
    load_global_instructions,
    render_global_instructions_section,
    render_instructions_section,
)
from ...events import NoticeEvent
from ...messages import Message
from ...models import Provider
from ...tools.memory import render_memory_section
from ._shared import EmitFn

if TYPE_CHECKING:
    # 循环引用的类型侧声明：backend_parts 不得运行期 import backend。这三个
    # 名字的定义在 backend.py（内核单一事实来源），运行期由 backend 在模块末尾
    # 单向注入本模块命名空间——此处只让静态检查看到绑定，方法体保持逐字搬移。
    from ..backend import HISTORY_SYNC_LIMIT, _msg_brief, _window_briefs


class SessionModelMixin:
    """会话模型 / 思考强度 / 分叉恢复 / 会话观测（D1 批 5b 外迁件）。

    方法自 backend.py 逐字搬入；状态全部来自主类共享 self（无 __init__）。
    """

    # ---- 分叉 / 恢复会话 ----

    async def fork_session(self, params: dict, local: bool = True) -> dict:
        """从某条消息分叉出新会话：复制 seq <= 锚点 的消息（缺省全部）。

        local=True（本机，默认）跨项目会话放行；分叉的新会话落回源会话
        所属的项目——跨项目分屏列的分叉不能把别家项目的对话复制进当前
        项目（快聊/当前项目会话保持既有口径）。"""
        sid = str(params.get("id", "") or (self.session.id if self.session else ""))
        sess = await self._get_owned_session(sid, local=local)
        # 与 truncate 同一守卫：轮末才批量落库，运行中分叉会复制到不完整的历史
        rt = self.runtimes.get(sid)
        if rt and rt.run_task and not rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再分叉")
        seq = int(params["seq"]) if params.get("seq") is not None else (
            await self.store.max_seq(sid) or 0)
        cur_pid = self._cur_project_id()
        fork_pid = sess.project_id if (
            sess.project_id is not None and sess.project_id != cur_pid
        ) else cur_pid
        new_sess = await self.store.create_session(
            fork_pid, title=(f"└ {sess.title or '分叉'}")[:40]
        )
        n = await self.store.copy_messages_between(sid, new_sess.id, seq)
        await self.store.touch(new_sess.id)
        # 克隆会话可能很大（源会话整体复制）：回包同样只下发尾部窗口，前端凭
        # messages_offset 继续经 messages.earlier 向前翻页
        msgs, total, offset = await self.store.load_messages_window(
            new_sess.id, limit=HISTORY_SYNC_LIMIT)
        return {
            "id": new_sess.id,
            "title": new_sess.title,
            "copied": n,
            "messages": [_msg_brief(m) for m in msgs],
            "messages_total": total,
            "messages_offset": offset,
        }

    async def resume_session(self, session_id: str) -> dict:
        sess = await self._get_owned_session(session_id)
        self.session = sess
        try:
            await self._write_ui_prefs({self.SESSION_ACTIVE_KEY: session_id})
        except Exception:  # noqa: BLE001
            pass
        rt = await self._get_runtime(session_id)
        msgs = await self._reload_agent_history(rt.agent, session_id)
        # runtime 历史仍全量装配（模型上下文需要）；回包只下发尾部窗口，
        # 更早的由前端经 messages.earlier 翻页（大会话切标签不再整包下发）
        briefs, total, offset = _window_briefs(
            [m for m in msgs if m.role in ("user", "assistant")])
        return {
            "id": sess.id, "title": sess.title, "summary": sess.summary,
            "messages": briefs,
            "messages_total": total,
            "messages_offset": offset,
        }

    # ---- 会话级模型（分屏独立选模型） ----

    async def session_model_switch(self, session_id: str, name: str, model: str) -> dict:
        """按会话切换模型（分屏独立对话）：只影响该会话 runtime 的 provider，
        不动全局——别的会话（含主栏）保持自己的模型。

        会话还没建 runtime 时先懒建（保证覆盖立即落在 agent 上）；覆盖记进
        _session_models，runtime 被淘汰重建后由 _runtime_provider 恢复。

        本机专属（WS 层 local_only）：跨项目分屏列换模型放行，runtime 绑定
        会话所属项目干活（_project_ctx_for）。"""
        await self._get_owned_session(session_id, local=True)
        name = str(name or "").strip()
        model = str(model or "").strip()
        if name not in self.cfg.providers:
            raise RuntimeError(f"未知服务：{name}")
        pc = self.cfg.providers[name]
        if model and pc.models and model not in pc.models:
            raise RuntimeError(f"服务 {name} 没有登记模型 {model}，先在模型服务里添加")
        existed = session_id in self.runtimes
        rt = await self._get_runtime(session_id)
        if not existed:
            # 懒建的是空壳 runtime（只有系统提示词）：照 resume_session 同款补拉
            # 历史。不补的话 runtime 已在内存，activate_session 的「不在 runtimes
            # 才重载」分支永不触发，此后该会话所有轮次都在空上下文上跑（重启或
            # LRU 淘汰后在分屏列换模型即触发——静默丢全部历史）。
            await self._reload_agent_history(rt.agent, session_id)
        rt.agent.provider = self._build_detached_provider(name, model or None)
        pc_eff = self.cfg.providers.get(name)
        rt.agent.context_limit_tokens = (
            pc_eff.effective_context_limit(self.cfg.context_limit_tokens)
            if pc_eff else self.cfg.context_limit_tokens
        )
        self._session_models[session_id] = (name, model)
        # 已存档位随热切迁移：新 provider 支持思考则重放；不支持则清除
        # （build_provider 已按新服务 config 设初值，两条路径命运一致）
        saved_effort = self._session_efforts.get(session_id)
        if saved_effort is not None:
            if getattr(rt.agent.provider, "supports_reasoning", False):
                rt.agent.provider.set_reasoning_effort(saved_effort)
            else:
                self._session_efforts.pop(session_id, None)
        return {
            "session_id": session_id,
            "provider": name,
            "model": model or pc.model,
            "supports_vision": True if pc_eff is None else bool(pc_eff.supports_vision),
            "context_limit": rt.agent.context_limit_tokens,
        }

    def session_model_get(self, session_id: str) -> dict:
        """会话当前生效的模型：有覆盖给覆盖，否则按三段解析（覆盖 → 新会话
        默认模型 → 全局）；supports_vision 随生效服务给（前端不再自行推导）。"""
        override = self._session_models.get(session_id)
        if override:
            name, model = override
            overridden = True
        else:
            name, model = self._session_effective_model(session_id)
            overridden = False
        pc = self.cfg.providers.get(name)
        return {
            "session_id": session_id,
            "name": name,
            "model": model or (pc.model if pc is not None else ""),
            "overridden": overridden,
            "supports_vision": True if pc is None else bool(pc.supports_vision),
        }

    # ---- 会话级思考强度 ----

    async def session_reasoning_get(self, session_id: str) -> dict:
        """会话当前生效的思考强度：不建 runtime，按三段解析出的服务读档位。

        有 runtime 读其 provider 实例（含热调未落盘的档位），无则读服务配置。
        overridden 表示该会话有会话级档位覆盖（session.reasoning_set 写入）。"""
        sess = await self.store.get_session(session_id)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        rt = self.runtimes.get(session_id)
        name, _model = self._session_effective_model(session_id)
        pc = self.cfg.providers.get(name)
        if rt is not None:
            supported = bool(getattr(rt.agent.provider, "supports_reasoning", False))
            effort = getattr(rt.agent.provider, "reasoning_effort", "auto")
        elif pc is not None:
            supported = bool(pc.supports_reasoning)
            effort = pc.reasoning_effort
        else:
            supported, effort = False, "auto"
        return {
            "session_id": session_id,
            "supported": supported,
            "effort": self._session_efforts.get(session_id, effort),
            "efforts": list(REASONING_EFFORTS),
            "labels": dict(REASONING_EFFORT_LABELS),
            "overridden": session_id in self._session_efforts,
        }

    async def session_reasoning_set(self, session_id: str, effort: str) -> dict:
        """按会话设置思考强度：不动全局、绝不静默更换会话模型。

        按三段解析出的 (服务, 模型) 用 detached 副本建独立 provider 挂回该
        会话 runtime（还没建就懒建，保证立即生效）——无覆盖会话的 agent
        原本与全局共享同一个 provider 对象，直接在共享对象上调档会把所有
        会话一起改掉，所以这里必须换成会话私有副本。能力校验 fail-closed
        （与 model.set_reasoning 同口径）；记入 _session_efforts，runtime
        重建后由 _get_runtime 恢复。"""
        sess = await self.store.get_session(session_id)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        effort = str(effort or "auto").strip().lower()
        if effort not in REASONING_EFFORTS:
            raise RuntimeError("思考强度只支持 " + " / ".join(REASONING_EFFORTS))
        name, model = self._session_effective_model(session_id)
        if not name:
            raise RuntimeError("当前没有可用的模型服务，先在模型服务里配置")
        pc = self.cfg.providers.get(name)
        existed = session_id in self.runtimes
        rt = await self._get_runtime(session_id)
        if not existed:
            # 懒建的是空壳 runtime：先补历史再校验（同 session_model_switch）。
            # 校验不过就把刚建的空壳回收——留着的话 activate_session 的重载
            # 分支不再触发，空壳会吃掉该会话之后的全部历史。
            await self._reload_agent_history(rt.agent, session_id)
        # 能力校验 fail-closed（与 model.set_reasoning 同口径）：配置里没有的
        # 服务（测试注入等）按 provider 实例判。校验不过就不动 runtime 的
        # provider——先换副本再发现不支持，会白白把共享 provider 私有化
        if pc is not None:
            supports = bool(pc.supports_reasoning)
        else:
            supports = bool(getattr(rt.agent.provider, "supports_reasoning", False))
        if not supports:
            if not existed:
                self.runtimes.pop(session_id, None)
                self._forget_runtime(rt)
            raise RuntimeError(f"「{name}」不支持调整思考强度（未声明该能力）")
        rt.agent.provider = self._build_detached_provider(name, model or None)
        rt.agent.provider.set_reasoning_effort(effort)
        pc_eff = pc if pc is not None else None
        rt.agent.context_limit_tokens = (
            pc_eff.effective_context_limit(self.cfg.context_limit_tokens)
            if pc_eff else self.cfg.context_limit_tokens
        )
        self._session_efforts[session_id] = effort
        return {
            "session_id": session_id,
            "supported": True,
            "effort": effort,
            "efforts": list(REASONING_EFFORTS),
            "labels": dict(REASONING_EFFORT_LABELS),
            "overridden": True,
        }

    # ---- 全局模型切换 ----

    async def switch_model(self, name: str, model: str | None = None) -> dict:
        self.provider = self._build_provider(name, model)
        self.provider_error = None
        limit = self._context_limit()
        private = self._model_private_sessions()
        # 单一循环按 sid 分派（避免两处循环口径漂移）：
        # - 模型身份私有（覆盖 ∪ 默认模型会话）→ 完全跳过，模型与上限都不动；
        # - 只覆盖档位 → 模型跟新全局：建 detached 副本并把已存档位重放
        #   （新服务不支持思考则清除档位，provider 回归共享全局对象）；
        # - 无覆盖（含基底）→ 共享全局 provider，上限刷新
        pairs: list[tuple[str | None, Agent]] = [(None, self._base_agent)]
        pairs.extend((rt.sid, rt.agent) for rt in self.runtimes.values())
        for sid, ag in pairs:
            if sid is not None and sid in private:
                continue
            saved_effort = self._session_efforts.get(sid) if sid is not None else None
            if saved_effort is not None:
                prov = self._build_detached_provider(name, model)
                if getattr(prov, "supports_reasoning", False):
                    prov.set_reasoning_effort(saved_effort)
                    ag.provider = prov
                else:
                    self._session_efforts.pop(sid, None)
                    ag.provider = self.provider
            else:
                ag.provider = self.provider
            # 换服务/换模型 = 换上下文窗口：上限与视觉能力都跟着新的服务走
            ag.context_limit_tokens = limit
        return {
            "provider": name, "model": self.provider_model,
            "reasoning": self.reasoning_state(),
            "context_limit": limit,
            "supports_vision": self._supports_vision(),
        }

    # ---- 新会话默认模型（设置/清除走 WS：default_model.set / default_model.get） ----

    async def set_default_model(self, params: dict) -> dict:
        """设定 / 清除「新会话默认模型」。

        name 传空串 = 清除（新会话回到跟随全局）；name+model = 只对这些
        **新创建**的会话用专用 provider，已存在的会话（含当前对话）不受影响。
        服务名必须在 config.toml 里存在，模型名必须是该服务已登记的模型
        （或留空 = 用该服务的默认模型）。"""
        name = str(params.get("name") or "").strip()
        model = str(params.get("model") or "").strip()
        if name:
            if name not in self.cfg.providers:
                raise RuntimeError(f"未知服务：{name}")
            pc = self.cfg.providers[name]
            if model and pc.models and model not in pc.models:
                raise RuntimeError(f"服务 {name} 没有登记模型 {model}，先在模型服务里添加")
        elif model:
            raise RuntimeError("清除默认模型时不能只留模型名")
        try:
            await self._write_ui_prefs({
                self.DEFAULT_MODEL_PROVIDER_KEY: name or None,
                "default_model_name": model or None,
            })
        except Exception as e:
            raise RuntimeError(f"写入偏好失败：{e}") from e
        return self.default_model_state()

    def default_model_state(self) -> dict:
        name, model = self._default_model_pref()
        return {
            "provider": name,
            "model": model,
            "label": (f"{name} / {model}" if name and model
                      else name if name else ""),
        }

    def _default_model_pref(self) -> tuple[str, str]:
        """读新会话默认模型偏好，返回 (provider, model)；未设置返回 ("", "")。"""
        try:
            prefs = self._read_ui_prefs()
        except Exception:  # noqa: BLE001
            return "", ""
        name = str(prefs.get(self.DEFAULT_MODEL_PROVIDER_KEY) or "").strip()
        if not name or name not in self.cfg.providers:
            return "", ""
        model = str(prefs.get("default_model_name") or "").strip()
        return name, model

    # ---- 辅助对话模型（设置/清除走 WS：aux.model.set / aux.model.get） ----

    def _aux_model_pref(self) -> tuple[str, str]:
        """辅助对话的专用模型偏好（ui.json）：(provider, model)，空 = 跟随主对话。

        服务可能已被删除、模型可能已被下架，读取时校验，失效即视为未设置。"""
        try:
            prefs = self._read_ui_prefs()
        except Exception:  # noqa: BLE001
            return "", ""
        name = str(prefs.get("aux_model_provider") or "").strip()
        if not name or name not in self.cfg.providers:
            return "", ""
        model = str(prefs.get("aux_model_name") or "").strip()
        pc = self.cfg.providers[name]
        if model and pc.models and model not in pc.models:
            model = ""
        return name, model

    def aux_model_state(self) -> dict:
        name, model = self._aux_model_pref()
        return {
            "provider": name,
            "model": model,
            "label": (f"{name} / {model}" if name and model
                      else name if name else ""),
            # 跟随主对话时入口徽章画主对话服务的 logo：给服务名而不是让前端
            # 拆 main_label 字符串；主模型切换后重开面板即刷新
            "main_provider": self.provider_name or "",
            "main_label": (f"{self.provider_name} / {self.provider_model}"
                           if self.provider_name else ""),
        }

    async def set_aux_model(self, params: dict) -> dict:
        """设定 / 清除「辅助对话模型」。

        name 传空串 = 清除（辅助对话回到跟随主对话）；name+model = 辅助面板
        用专用 provider 一问一答，不影响主对话与各会话的 runtime。服务名必须
        在 config.toml 里存在，模型名必须是该服务已登记的模型（或留空 = 用
        该服务的默认模型）。"""
        name = str(params.get("name") or "").strip()
        model = str(params.get("model") or "").strip()
        if name:
            if name not in self.cfg.providers:
                raise RuntimeError(f"未知服务：{name}")
            pc = self.cfg.providers[name]
            if model and pc.models and model not in pc.models:
                raise RuntimeError(f"服务 {name} 没有登记模型 {model}，先在模型服务里添加")
        elif model:
            raise RuntimeError("清除辅助对话模型时不能只留模型名")
        try:
            await self._write_ui_prefs({
                "aux_model_provider": name or None,
                "aux_model_name": model or None,
            })
        except Exception as e:
            raise RuntimeError(f"写入偏好失败：{e}") from e
        return self.aux_model_state()

    # ---- 思考强度（全局档位） ----

    def reasoning_state(self) -> dict:
        """当前思考强度状态：是否支持 + 当前档位 + 各服务自己的档位。"""
        cur = getattr(self.provider, "reasoning_effort", "auto")
        return {
            "supported": bool(getattr(self.provider, "supports_reasoning", False)),
            "effort": cur,
            "efforts": list(REASONING_EFFORTS),
            "labels": dict(REASONING_EFFORT_LABELS),
            "providers": {
                name: {
                    "supports_reasoning": bool(pc.supports_reasoning),
                    "effort": pc.reasoning_effort,
                }
                for name, pc in self.cfg.providers.items()
            },
        }

    async def set_reasoning_effort(self, effort: str, name: str | None = None) -> dict:
        """调整思考强度并热生效。

        name 缺省 = 当前使用的服务；写入 config.toml 的对应 provider 后重建当前
        provider，让下一轮对话立即用新档位。不支持该能力的服务直接报错。
        """
        target = name or self.provider_name
        effort = str(effort or "auto").strip().lower()
        if effort not in REASONING_EFFORTS:
            raise RuntimeError("思考强度只支持 " + " / ".join(REASONING_EFFORTS))
        pc = self.cfg.providers.get(target)
        # 当前 provider（含测试注入/自定义装配）即使不在配置表里也允许直接调档
        is_current = bool(self.provider is not None and target == self.provider_name)
        if pc is None and not is_current:
            raise RuntimeError("unknown provider: " + str(target))
        supports = bool(pc.supports_reasoning) if pc is not None else bool(
            getattr(self.provider, "supports_reasoning", False)
        )
        if not supports:
            raise RuntimeError(f"「{target}」不支持调整思考强度（未声明该能力）")
        if pc is not None:
            update_provider_in_config(target, reasoning_effort=effort)
            self.cfg = load_config()
        if is_current and self.provider is not None:
            # 不重建整个 provider（避免丢 Key/连接），直接改档位即可生效
            self.provider.set_reasoning_effort(effort)
            # 会话已私有（模型身份或档位任一）的会话不施全局档位：它们挂的
            # 不是共享的 provider 对象，各自档位走 session.reasoning_set 面。
            # 基底（无会话）与无覆盖会话照旧共享全局 provider，直接换档。
            private = self._model_private_sessions() | set(self._session_efforts)
            self._base_agent.provider = self.provider
            for rt in self.runtimes.values():
                if rt.sid in private:
                    continue
                rt.agent.provider = self.provider
        return {"provider": target, "reasoning": self.reasoning_state()}

    # ---- 模型三段解析与 provider 生命周期 ----

    def _model_private_sessions(self) -> set[str]:
        """模型身份私有的会话：会话级模型覆盖 ∪ 生效的「新会话默认模型」会话。

        这些会话的模型不跟全局（全局切换 model.switch 对它们完全跳过）；
        「只覆盖档位」的会话（_session_efforts 有、这里没有）不在此列——
        它们的模型跟全局，切换时档位随迁。"""
        sids = set(self._session_models)
        # 默认模型会话按创建时刻的快照记（不随偏好现状翻转）
        sids |= set(getattr(self, "_default_model_sessions", {}))
        return sids

    def _session_effective_model(self, session_id: str) -> tuple[str, str]:
        """会话当前生效的 (服务名, 模型名)，三段解析的单处实现：

        1. 会话级模型覆盖（分屏独立选的模型）；
        2. 「新会话默认模型」会话（创建时刻的 (服务, 模型) 快照）；
        3. 全局记账（provider_name / provider_model）。

        回退口径与 _runtime_provider 逐字一致：覆盖指向的服务已不存在即清除
        覆盖并落往下一段；默认模型快照里的服务已不存在同样清除并落往下段。
        全局腿可靠的前提是 _runtime_provider 全程 detached 构建（见上）。"""
        override = self._session_models.get(session_id)
        if override:
            if override[0] in self.cfg.providers:
                return override
            self._session_models.pop(session_id, None)  # 覆盖失效即清除
        # 默认模型腿按创建时刻的快照回读（与 _runtime_provider 同源）；快照里的
        # 服务已不存在（被删/停用）即清除标记、回落全局
        baked = getattr(self, "_default_model_sessions", {}).get(session_id)
        if baked:
            if baked[0] in self.cfg.providers:
                return baked
            self._default_model_sessions.pop(session_id, None)
        return self.provider_name or "", self.provider_model or ""

    def _runtime_provider(self, session_id: str):
        """会话的起始 provider：会话级覆盖（分屏独立选的模型）优先，其次新会话
        默认模型，最后跟随全局。覆盖模型不可用（服务被删等）时回退全局，
        不让发消息失败。"""
        override = self._session_models.get(session_id)
        if override:
            try:
                return self._build_detached_provider(override[0], override[1] or None)
            except Exception:  # noqa: BLE001
                self._session_models.pop(session_id, None)  # 覆盖失效即清除
        # 「新会话默认模型」腿只看创建时刻的快照（new_session() 记下）：偏好
        # 后来被清除/更改也不影响已存在的会话（set_default_model 的文档语义）
        baked = getattr(self, "_default_model_sessions", {}).get(session_id)
        if not baked:
            return self.provider
        # detached 构建：不改写 provider_name / provider_model（「当前使用服务」
        # 的全局记账）。此前这条腿走 _build_provider，每次新会话预建 runtime 都把
        # 记账改指默认服务、provider 对象却还是旧服务——与 set_default_model
        # 「已存在的会话（含当前对话）不受影响」的文档语义相悖，也让三段解析的
        # 全局腿（self.provider_name）失真。改用 detached 后，全局记账只在
        # 真全局切换（model.switch）时变化。
        try:
            return self._build_detached_provider(baked[0], baked[1] or None)
        except Exception:  # noqa: BLE001
            return self.provider  # 预设被删/停用时回退全局，不让发消息失败

    async def _apply_model_routing(
        self, agent: Agent, sid: str, text: str, *, regenerate: bool, emit: EmitFn,
    ) -> tuple[Provider | None, Provider | None]:
        """A3 模型自动分层路由：按本轮复杂度临时换 provider，返回 (原provider, 本轮provider)。

        三条边界（都不满足就原样返回 (None, None)）：
        - 总开关关着、或该档没配服务：不动；
        - 本会话有**会话级模型覆盖**（用户在下拉里手动选过）：用户的决定优先，不动；
        - 团队/圆桌/对抗轮根本不会走到这里（多模型协作的各角色模型由模板决定）。

        复杂度信号复用思考强度自动档的 estimate_effort（纯启发式、零额外请求）：
        low → 轻量模型，high → 主力模型（没配就留在会话模型），medium → 不路由。
        换上去的 provider 是本轮临时构建的独立实例，轮末由调用方还原并释放。
        """
        rc = getattr(self.cfg, "routing", None)
        if rc is None or not bool(getattr(rc, "enabled", False)):
            return None, None
        if sid in self._session_models:
            return None, None  # 手动选过模型：不替用户做主
        try:
            probe = list(agent.history)
            if not regenerate and text:
                probe = [*probe, Message.user(text)]
            level = estimate_effort(probe)
        except Exception:  # noqa: BLE001 - 估档失败就不路由，不影响本轮
            return None, None
        if level == "low":
            name = str(getattr(rc, "light_provider", "") or "").strip()
            model = str(getattr(rc, "light_model", "") or "").strip()
            tier = "轻量"
        elif level == "high":
            name = str(getattr(rc, "heavy_provider", "") or "").strip()
            model = str(getattr(rc, "heavy_model", "") or "").strip()
            tier = "主力"
        else:
            return None, None
        if not name or name not in self.cfg.providers:
            return None, None
        try:
            prov = self._build_detached_provider(name, model or None)
        except Exception:  # noqa: BLE001 - 服务不可用就留在会话模型
            return None, None
        prev = agent.provider
        agent.provider = prov
        try:
            # 管线里的 emit 收的是事件 dict（与 agent.run_turn 的 model_dump 同口径）
            await emit(NoticeEvent(message=(
                f"本轮按任务复杂度走{tier}模型（{name}{'/' + model if model else ''}）："
                "设置 · 高级 · 模型自动分层 可关。"
            )).model_dump())
        except Exception:  # noqa: BLE001 - 提示发不出不影响本轮
            pass
        return prev, prov

    def _release_turn_providers(self, provs, keep=()) -> None:
        """释放「本轮临时构建」的成员 provider（圆桌/对抗/团队成员）。

        这些 provider 每轮由 _build_member_provider 新建（各自带 httpx 连接池），
        轮结束就没人再用了。跳过 keep 里的常驻对象（如圆桌主席、主 Agent 的
        provider——它们是会话/全局对象，不归本轮管）与全局共享 provider。
        释放挂到「当前任务完成之后」（当前任务即本轮的 run_task）：正常路径下
        成员流已收束，二次取消的窄竞态窗口里也保证不倒在途流一把（关 httpx
        会把在飞请求变噪音异常，等轮任务全落地再关最稳）。
        """
        kept = {id(p) for p in keep if p is not None}
        kept.add(id(self.provider))
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            cur = None
        seen: set[int] = set(kept)
        for p in provs:
            if p is None or id(p) in seen:
                continue  # 同一实例可身兼数角（对抗自动补位）：只关一次
            seen.add(id(p))
            self._close_provider_after(p, cur)

    def _close_provider_after(self, prov, run_task=None) -> None:
        """释放被弃用的会话私有 provider 的连接池（httpx AsyncClient）。

        只处理「会话私有副本」：全局共享 provider（self.provider）不归单会话
        管——还挂着别的会话、甚至可能有在途流，换了也不能关。测试注入的假
        provider 继承基类的空 aclose，调了也无害。
        时序：会话还有未收尾的轮次（如删会话时先 cancel 后回收）时，把关闭
        挂到该任务完成之后——在途流被客户端关闭会让收尾拿到奇怪的异常；
        轮次落地后再关，两者互不打扰。关闭走后台任务，失败无感。
        """
        if prov is None or prov is self.provider:
            return
        aclose = getattr(prov, "aclose", None)
        if not callable(aclose):
            return

        def _fire() -> None:
            try:
                spawn_bg(aclose())
            except RuntimeError:
                pass  # 无运行中的事件循环（同步测试路径）：跳过释放

        if run_task is not None and not run_task.done():
            run_task.add_done_callback(lambda _t: _fire())
        else:
            _fire()

    # ---- 会话观测（/compact /status 与上下文构成明细） ----

    async def compact_now(self) -> dict:
        """手动触发上下文压缩（/compact）。没有可压缩内容时返回 compacted=False。"""
        agent = self.agent
        ev = await compact_history(agent, keep_recent=self.cfg.compaction_keep_recent)
        return {
            "compacted": ev is not None,
            "before": ev.before_messages if ev else 0,
            "after": ev.after_messages if ev else 0,
            "summary_chars": ev.summary_chars if ev else 0,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
            "context_detail": self._context_detail(agent),
        }

    def _context_detail(self, agent: Agent) -> dict:
        """上下文构成明细（输入栏环形仪表的悬停弹层）：按消息 / 系统提示词 /
        技能 / 工具分桶估算 token 占用，附会话累计的平均缓存命中率。
        纯估算、仅供界面参考；「其他」吸收图片输入与各家 tokenizer 的估算偏差
        （真实上报 − 各桶估算之和，负值归零）。"""
        sys_text = (
            build_system_prompt(self.working_dir)
            + render_instructions_section(self.instructions_file, self.instructions_text)
            + render_global_instructions_section(load_global_instructions())
            + render_memory_section()
        )
        skills_text = self.skills.render_prompt_section() if self.skills else ""
        mcp_names = {t.name for t in (self.mcp_tools or [])}
        # registry 为 None = runtime 已被 _forget_runtime 释放（切项目/删会话时
        # 被取消的轮还在收尾）：此时拿不到工具清单，按空表估算，别让收尾炸掉
        schemas = agent.registry.schemas() if agent.registry is not None else []
        buckets = [
            ("消息", estimate_tokens([m for m in agent.history if m.role != "system"])),
            ("系统工具", estimate_text_tokens(json.dumps(
                [s for s in schemas if s["name"] not in mcp_names], ensure_ascii=False))),
            ("技能", estimate_text_tokens(skills_text)),
            ("系统提示词", estimate_text_tokens(sys_text)),
            ("MCP 工具", estimate_text_tokens(json.dumps(
                [s for s in schemas if s["name"] in mcp_names], ensure_ascii=False))),
        ]
        total = agent.used_context_tokens()
        rest = total - sum(n for _, n in buckets)
        buckets.append(("其他", max(0, rest)))
        denom = sum(n for _, n in buckets) or 1
        rows = [
            {"label": label, "tokens": n, "pct": round(100 * n / denom, 1)}
            for label, n in sorted(buckets, key=lambda kv: -kv[1])
        ]
        rate = None
        if agent.total_cached_tokens > 0 and agent.total_in_tokens > 0:
            # 服务从未上报过缓存明细时保持 None（前端显示「—」），不冒充 0%
            rate = round(100 * agent.total_cached_tokens / agent.total_in_tokens, 1)
        return {"tokens": total, "limit": agent.context_limit_tokens,
                "rows": rows, "cache_rate": rate}

    async def status(self, session_id: str | None = None) -> dict:
        """/status：模型、上下文占用、工具数、任务清单一览。

        session_id 给出时查那个会话的 runtime（分屏列辅路：peek 不建 runtime）。
        无 runtime（重启 / LRU 淘汰后）不直接报错：会话在库时按落库消息估算
        占用回给上下文徽标——否则打开旧会话的列要等该列发过一条消息才有读数；
        估算口径与 agent.used_context_tokens 的估算半边一致。会话不在库仍显式
        报错，绝不静默回退当前会话（前端据此保持徽标空态）。"""
        if session_id:
            rt = self.runtimes.get(session_id)
            if rt is None:
                est = await self._estimate_session_context(session_id)
                if est is None:
                    raise RuntimeError(
                        f"该会话没有正在运行的上下文（可能已被回收）：{session_id}")
                tokens, history_n = est
                return {
                    "version": __version__,
                    "working_dir": str(self.working_dir or ""),
                    "provider": self.provider_name,
                    "model": self.provider_model,
                    "provider_error": self.provider_error,
                    "session_id": session_id,
                    "context_tokens": tokens,
                    "context_limit": self._context_limit(),
                    # 明细桶（系统提示词/工具/技能分摊）离线还原不了：给 None，
                    # 前端容量弹层按无明细处理，环读数本身不受影响
                    "context_detail": None,
                    "history_messages": history_n,
                    "tool_count": len(self.agent.registry),
                    "queued": 0,
                    "todos": [],
                }
            agent, sid, queued = rt.agent, session_id, len(rt.queue)
        else:
            agent = self.agent
            sid = self.session.id if self.session else None
            queued = len(self.queue)
        todo_tool = agent.registry.get("todo_write")
        return {
            "version": __version__,
            "working_dir": str(self.working_dir or ""),
            "provider": self.provider_name,
            "model": self.provider_model,
            "provider_error": self.provider_error,
            "session_id": sid,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
            "context_detail": self._context_detail(agent),
            "history_messages": len(agent.history),
            "tool_count": len(agent.registry),
            "queued": queued,
            "todos": list(getattr(todo_tool, "items", []) or []),
        }

    async def _estimate_session_context(self, session_id: str) -> tuple[int, int] | None:
        """无 runtime 会话的上下文占用估算（分屏列徽标辅路）。

        会话不在库、或没有任何落库消息（刚建的空会话 / 任务壳会话）返回 None，
        由调用方维持显式报错的空态契约；有历史则按落库消息以 estimate_tokens
        估算（与 agent.used_context_tokens 的估算半边同口径），返回 (tokens, 条数)。"""
        if self.store is None:
            return None
        session = await self.store.get_session(session_id)
        if session is None:
            return None
        msgs = await self.store.load_messages(session_id)
        if not msgs:
            return None
        return estimate_tokens(msgs), len(msgs)
