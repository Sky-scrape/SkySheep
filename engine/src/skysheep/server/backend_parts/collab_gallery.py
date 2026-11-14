"""协作模板画廊：内置三张 + 用户圆桌/对抗 + 团队模板统一画廊。

从 backend.py 拆出（10k 行巨石）。画廊把三种模板统一成 CollabTemplate 形状
读写：用户圆桌/对抗住 collab_templates.json，团队模板仍住 teams.json。
schema 里没有 gate / auto_accept / permission——模板只能声明 needs 与工具
白名单，运行时授权由 GrantedGate 与会话门取严，模板数据永远不能关闭或降低
权限门。
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from ...config import resolve_api_key
from ...core.collab_templates import (
    COLLAB_NEEDS,
    PREVIEW_INPUT_FLOOR_TOKENS,
    PREVIEW_OUTPUT_TOKENS,
    CollabTemplate,
    CollabTemplateError,
    builtin_template,
    derive_needs,
    effective_tool_entries,
    is_builtin_template_name,
    normalize_command_prefix,
    validate_collab_template,
)
from ...core.context import estimate_text_tokens
from ...core.team_store import (
    TeamTemplate,
    TeamTemplateDirector,
    TeamTemplateMember,
)
from ...security.gate import _RUN_COMMAND
from ...tools.base import Safety, ToolRegistry
from .collab_team_templates import (
    _CollabTeamTemplate,
    _CollabTeamTemplateBudget,
)


class CollabGalleryMixin:
    # ---- 协作模板（本期）：内置三张 + 用户圆桌/对抗 + 团队模板统一画廊 ----
    # 协作模板统一放在 CollabTemplate 形状下读写：用户圆桌/对抗住
    # collab_templates.json（CollabTemplateStore），团队模板仍住 teams.json
    # （TeamTemplateStore，本期只加可选 needs/tools/budget 字段，不迁移）。
    # schema 里没有 gate / auto_accept / permission——模板只能声明 needs 与
    # 工具白名单，运行时授权由 GrantedGate 与会话门取严，模板数据永远不能
    # 关闭或降低权限门。

    def _normalize_template_dict(self, data: dict) -> dict:
        """把调用方（测试/内部）直传的模板 dict 规范成统一形状。

        ``builtin`` 标记不可信：只有代码里的内置模板才有内置身份；用户传来的
        同名字段一律抹掉（内置名另由 validate_collab_template 拒绝）。
        """
        payload = dict(data or {})
        try:
            template = CollabTemplate(**payload)
        except ValidationError as e:
            first = e.errors()[0] if e.errors() else {}
            loc = ".".join(str(x) for x in (first.get("loc") or ()))
            raise CollabTemplateError(
                f"模板数据格式不正确：{loc or '字段'} {first.get('msg', '')}".strip()
            ) from e
        template.builtin = False
        # builtin 是调用方可控字段，绝不能拿来放宽校验（审查 finding 9）：
        # 只允许代码内 _builtin_specs() 走 allow_builtin=True；chat.send
        # 直传的同名字段一律无效，内置名保留与「至少一个角色」照常校验。
        return validate_collab_template(template, allow_builtin=False).model_dump()

    def _team_template_to_collab(self, template: TeamTemplate) -> dict:
        """团队模板 → 协作模板形状（画廊/预览统一口径）。

        团队模板只存团队级 needs/tools（本期不做逐成员隔离）：反向生成角色时
        每个角色都带团队级声明，并在 preview 的 role_scope_note 里明示。
        """
        needs = str(getattr(template, "needs", "read") or "read")
        if needs not in COLLAB_NEEDS:
            needs = "read"
        tools_allow = list(getattr(template, "tools_allow", []) or [])
        tools_deny = list(getattr(template, "tools_deny", []) or [])
        budget_raw = getattr(template, "budget", None)
        if hasattr(budget_raw, "model_dump"):
            budget = budget_raw.model_dump()
        elif isinstance(budget_raw, dict):
            budget = dict(budget_raw)
        else:
            budget = {"max_calls": 0, "max_tokens": 0, "max_seconds": 0}
        director = template.director
        aggregator = (
            {"provider": director.provider, "model": director.model, "prompt": ""}
            if template.director_mode == "ai" and director.provider
            else {"provider": "", "model": "", "prompt": ""}
        )
        roles = []
        for i, member in enumerate(template.members or []):
            roles.append({
                "key": f"m{i}",
                "name": member.name or f"成员{i + 1}",
                "provider": member.provider,
                "model": member.model,
                "prompt": member.persona,
                "needs": needs,
                "tools_allow": list(tools_allow),
                "tools_deny": list(tools_deny),
            })
        return {
            "name": template.name,
            "kind": "team",
            # 团队模板自己的说明（_CollabTeamTemplate.desc，teams.json 随行落盘）；
            # 基类 TeamTemplate 没有该字段时 getattr 兜底为空
            "desc": str(getattr(template, "desc", "") or ""),
            "roles": roles,
            "aggregator": aggregator,
            "limits": {
                "max_members": 0, "member_timeout_s": 0, "debate_rounds": 0,
                "max_findings": 0, "role_timeout_s": 0,
            },
            "budget": budget,
            "created_at": float(template.created_at or 0.0),
            "builtin": False,
            "source": "team",
        }

    def _collab_to_team_template(self, template: CollabTemplate) -> _CollabTeamTemplate:
        """协作模板（kind=team）→ teams.json 的团队模板条目。

        角色 → members（name/persona），收口 → director（provider 为空 = 用户
        总管），团队级 needs 取角色最高档，工具取并集（deny 优先）。
        """
        aggregator = template.aggregator
        director_mode = "ai" if aggregator.provider else "user"
        needs = derive_needs(template.roles)
        tools_allow = effective_tool_entries(template.roles)
        tools_deny: list[str] = []
        for role in template.roles:
            for entry in role.tools_deny:
                if entry not in tools_deny:
                    tools_deny.append(entry)
        return _CollabTeamTemplate(
            name=template.name,
            desc=template.desc,
            director_mode=director_mode,
            director=TeamTemplateDirector(
                provider=aggregator.provider if director_mode == "ai" else "",
                model=aggregator.model if director_mode == "ai" else "",
            ),
            members=[
                TeamTemplateMember(
                    provider=role.provider, model=role.model,
                    name=role.name, persona=role.prompt,
                )
                for role in template.roles
            ],
            created_at=time.time(),
            needs=needs,
            tools_allow=tools_allow,
            tools_deny=tools_deny,
            budget=_CollabTeamTemplateBudget(**template.budget.model_dump()),
        )

    def _find_collab_template(
        self, name: str, source: str | None = None,
    ) -> dict | None:
        """按名在「内置 + 协作册 + 团队册」三处解析，返回统一模板 dict。

        两册同名（只能是手改 JSON 造出来的）且未给 source 时抛中文歧义错误；
        source 与解析结果不符按找不到处理（调用方给「模板来源不匹配」）。
        """
        raw = str(name or "").strip()
        if not raw:
            return None
        builtin = builtin_template(raw)
        user_collab = next(
            (t for t in self.collab_templates.templates if t.name == raw), None
        )
        team = next(
            (t for t in self.team_templates.templates if t.name == raw), None
        )
        matches: list[tuple[str, object]] = []
        if builtin is not None:
            matches.append(("collab", builtin))
        if user_collab is not None:
            matches.append(("collab", user_collab))
        if team is not None:
            matches.append(("team", team))
        wanted = str(source or "").strip()
        if wanted:
            matches = [pair for pair in matches if pair[0] == wanted]
        elif len(matches) > 1:
            raise CollabTemplateError(
                "同名模板同时存在于两个模板册，请指明来源（source）"
            )
        if not matches:
            return None
        src, obj = matches[0]
        if src == "team":
            return self._team_template_to_collab(obj)  # type: ignore[arg-type]
        payload = obj.model_dump()  # type: ignore[union-attr]
        payload["source"] = "collab"
        return payload

    def _collab_template_warnings(self, template: CollabTemplate) -> list[str]:
        """保存期的「注册表感知」校验（只对工具表里已知的工具名）。

        - read 角色列了写/执行工具 → 拒绝（声明与工具面自相矛盾）；
        - write 角色列了 DANGEROUS（run_command 前缀除外）→ 拒绝；
        - 未知工具名 → 不拒绝，回一条 warning（MCP/技能工具可能临时不可用）。
        """
        warnings: list[str] = []
        try:
            registry = self._build_full_registry()
        except Exception as e:  # noqa: BLE001 - 注册表构建失败不阻断保存
            return [f"当前工具表不可用，已跳过工具名检查：{str(e)[:120]}"]
        for role in template.roles:
            for entry in role.tools_allow:
                base = entry.split(":", 1)[0]
                tool = registry.get(base)
                if tool is None:
                    warnings.append(
                        f"工具 {entry} 当前工具表里不存在，运行时会被忽略"
                    )
                    continue
                if role.needs == "read" and tool.safety != Safety.READONLY:
                    raise CollabTemplateError(
                        f"角色「{role.name}」声明只读，但列了写/执行工具 {base}："
                        "请把它移到需要写入的角色，或把该角色 needs 改为 write"
                    )
                if (
                    role.needs == "write"
                    and tool.safety == Safety.DANGEROUS
                    and base != _RUN_COMMAND
                ):
                    raise CollabTemplateError(
                        f"角色「{role.name}」的 needs 是 write，不能列执行类工具 {base}"
                    )
        return warnings

    def collab_template_list(self) -> dict:
        """collab.template_list：内置三张 + 用户协作模板 + 团队模板（统一形状）。"""
        templates: list[dict] = []
        for item in self.collab_templates.list():
            payload = dict(item)
            payload["source"] = "collab"
            templates.append(payload)
        team_templates = [
            self._team_template_to_collab(t) for t in self.team_templates.templates
        ]
        return {
            "templates": templates,
            "team_templates": team_templates,
            "skipped": int(getattr(self.collab_templates, "skipped", 0) or 0),
        }

    async def collab_template_save(self, params: dict) -> dict:
        """collab.template_save：保存协作模板（kind 决定落哪一册）。

        - kind=roundtable / adversarial → collab_templates.json；
        - kind=team → teams.json（沿用团队模板册，不迁移）；角色数超过
          [team].max_members 直接拒绝（建队时的静默截断对用户不可见）；
        - 内置名保留；跨册重名拒绝（两册的 remove/preview 都按 source 消歧）；
        - ``source`` 可省略（由 kind 推导），给了但不相符按来源不匹配报错。
        """
        raw = params.get("template")
        if not isinstance(raw, dict):
            raw = params
        try:
            template = CollabTemplate(**raw)
        except ValidationError as e:
            first = e.errors()[0] if e.errors() else {}
            loc = ".".join(str(x) for x in (first.get("loc") or ()))
            raise CollabTemplateError(
                f"模板数据格式不正确：{loc or '字段'} {first.get('msg', '')}".strip()
            ) from e
        template.builtin = False
        validate_collab_template(template, allow_builtin=False)
        kind = template.kind
        expected_source = "team" if kind == "team" else "collab"
        given_source = str(params.get("source") or "").strip()
        if given_source and given_source != expected_source:
            raise CollabTemplateError(
                f"模板来源不匹配：{kind} 模板应保存到 {expected_source} 册"
            )
        if kind == "team":
            if any(t.name == template.name for t in self.collab_templates.templates):
                raise CollabTemplateError(
                    f"已有同名的协作模板：{template.name}，请换一个名字"
                )
            limit = max(1, int(getattr(self.cfg.team, "max_members", 3) or 3))
            if len(template.roles) > limit:
                raise CollabTemplateError(
                    f"模板有 {len(template.roles)} 个角色，超过团队上限 {limit}"
                    "（可在 设置 · 团队 调高，或减少角色）"
                )
            warnings = self._collab_template_warnings(template)
            self.team_templates.upsert(self._collab_to_team_template(template))
            return {
                "template": template.model_dump(),
                "source": "team",
                "warnings": warnings,
                "skipped_reason": None,
            }
        if any(t.name == template.name for t in self.team_templates.templates):
            raise CollabTemplateError(
                f"已有同名的团队模板：{template.name}，请换一个名字"
            )
        warnings = self._collab_template_warnings(template)
        if template.created_at <= 0:
            template.created_at = time.time()
        self.collab_templates.upsert(template)
        return {
            "template": template.model_dump(),
            "source": "collab",
            "warnings": warnings,
            "skipped_reason": None,
        }

    async def collab_template_remove(self, params: dict) -> dict:
        """collab.template_remove：按名删协作模板（内置不可删；两册同名要 source）。"""
        name = str(params.get("name") or "").strip()
        if not name:
            raise CollabTemplateError("协作模板名不能为空")
        if is_builtin_template_name(name):
            raise CollabTemplateError(f"内置模板不能删除：{name}")
        source = str(params.get("source") or "").strip()
        if source and source not in ("collab", "team"):
            raise CollabTemplateError("模板来源只能是 collab / team")
        in_collab = any(t.name == name for t in self.collab_templates.templates)
        in_team = any(t.name == name for t in self.team_templates.templates)
        if source == "collab":
            if not in_collab:
                raise CollabTemplateError(f"找不到协作模板: {name}")
            self.collab_templates.remove(name)
        elif source == "team":
            if not in_team:
                raise CollabTemplateError(f"找不到协作模板: {name}")
            self.team_templates.remove(name)
        elif in_collab and in_team:
            raise CollabTemplateError(
                "同名模板同时存在于两个模板册，请指明来源（source）"
            )
        elif in_collab:
            self.collab_templates.remove(name)
        elif in_team:
            self.team_templates.remove(name)
        else:
            raise CollabTemplateError(f"找不到协作模板: {name}")
        return {"name": name, "source": source or ("collab" if in_collab else "team")}

    def _preview_auto_pool(self) -> list[tuple[str, str]]:
        """已配置 Key 的服务池（模板里 provider 留空时的自动补位来源）。

        与真实建队共用 `_auto_pool_services`：健康度排序在预览里也要一致，
        否则清单上写的「自动挑选」与实际落位顺序会不一致。
        """
        return self._auto_pool_services()

    def _preview_team_members(self, roles: list[dict]) -> tuple[int, list[str]]:
        """轻量复刻 _template_team_specs 的补位与去重（不构建 Provider）。

        返回 (effective_member_count, dropped_role_names)：与真实建队同一
        （服务, 模型）去重键，空键（没有任何可用服务）不去重。
        """
        pool = self._preview_auto_pool()
        cursor = 0
        seen: set[tuple[str, str]] = set()
        count = 0
        dropped: list[str] = []
        for role in roles or []:
            if not isinstance(role, dict):
                continue
            name = str(role.get("name") or "").strip() or "队员"
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            if provider_name:
                if not model:
                    pc = self.cfg.providers.get(provider_name)
                    model = getattr(pc, "model", "") if pc is not None else ""
                key = (provider_name, model)
            elif pool:
                src = pool[cursor % len(pool)]
                cursor += 1
                key = src
            else:
                key = ("", "")
            if key in seen:
                dropped.append(name)
                continue
            seen.add(key)
            count += 1
        return count, dropped

    def _preview_tool_entries(
        self, template: dict, needs: str,
    ) -> tuple[list[dict], list[str], list[str]]:
        """工具并集 → 预览条目（含 mode/note）、命令前缀、永远逐次确认清单。"""
        try:
            registry: ToolRegistry | None = self._build_full_registry()
        except Exception:  # noqa: BLE001 - 注册表不可用时按「未知工具」展示
            registry = None
        entries = effective_tool_entries(template.get("roles") or [])
        tools: list[dict] = []
        prefixes: list[str] = []
        for entry in entries:
            base, _sep, prefix = entry.partition(":")
            tool = registry.get(base) if registry is not None else None
            known = tool is not None
            # 每行独立：note_ask / grant_dependent 只对「勾选与否改变放行结果」的行
            # 赋值，不能把上一行的值带进下一行
            note_ask = ""
            grant_dependent = False
            if base == _RUN_COMMAND:
                if prefix:
                    # 手改 JSON 可能绕过保存期提炼：预览再做一次安全校验，
                    # 不安全的原始前缀如实标 confirm（运行时 GrantedGate 会丢弃）
                    try:
                        prefix = normalize_command_prefix(prefix)
                    except CollabTemplateError:
                        mode = "confirm"
                        note = "命令前缀未通过安全提炼（含拼接/解释器），运行时会被忽略"
                        tools.append({
                            "name": base, "command_prefix": prefix,
                            "known": known, "mode": mode, "note": note,
                        })
                        continue
                    if needs == "exec":
                        mode = "auto"
                        note = "仅此命令前缀免确认；拼接/代码执行旗标仍逐次确认"
                        note_ask = "未勾选免确认：命令执行仍逐次确认（勾选后仅此前缀免确认）"
                        grant_dependent = True
                    else:
                        mode = "confirm"
                        note = f"团队 needs={needs}，运行时不会放行命令执行"
                    if prefix not in prefixes:
                        prefixes.append(prefix)
                else:
                    mode = "confirm"
                    note = "run_command 必须写成 run_command:<命令前缀>"
            elif not known:
                mode = "confirm"
                note = "当前工具表里没有这个工具，运行时会被忽略"
            elif tool.safety == Safety.READONLY:
                mode = "auto"
                note = "只读工具自动放行"
                # 只读工具不看「本次不再逐次确认」复选框：基类门本就自动放行，
                # 未勾选时也不能把「本次免确认」改写成「逐次确认」（走查 collab-5）
                grant_dependent = False
            elif tool.safety == Safety.WRITE:
                if needs in ("write", "exec"):
                    mode = "auto"
                    note = "工作目录内写入免确认；引擎主目录/目录外仍逐次确认"
                    note_ask = "未勾选免确认：写入仍逐次确认（勾选后工作目录内写入免确认）"
                    grant_dependent = True
                else:
                    mode = "confirm"
                    note = f"团队 needs={needs}，运行时不挂写工具"
            else:  # DANGEROUS
                mode = "confirm"
                note = "执行/控制类工具本次运行仍需逐次确认"
            entry_row = {
                "name": base,
                "command_prefix": prefix,
                "known": known,
                "mode": mode,
                "note": note,
                # 该行的「免确认」是否取决于跑前清单复选框：前端据此只联动这些行，
                # 只读工具的 auto 与复选框无关（未勾选也照常放行）
                "grant_dependent": grant_dependent,
            }
            if note_ask:
                entry_row["note_ask"] = note_ask
            tools.append(entry_row)
        always_confirm: list[str] = []
        if registry is not None:
            always_confirm = sorted(
                t.name for t in registry.all()
                if t.safety == Safety.DANGEROUS and t.name != _RUN_COMMAND
            )
        return tools, prefixes, always_confirm

    def _preview_pricing_calls(
        self, template: dict, estimated_calls: int, debate_rounds: int,
    ) -> tuple[list[tuple[str, str]], bool]:
        """估算调用清单 [(provider, prompt)] 与「是否有角色未指定服务」。

        圆桌：每个角色 (1+辩论轮) 次 + 收口 1 次；对抗：最多三个角色各 1 次 +
        裁判 1 次；团队：max_calls 按角色轮转分摊（无法知道真实派工分布，
        cost_basis 里如实注明是粗估）。
        """
        kind = str(template.get("kind") or "")
        roles = [r for r in (template.get("roles") or []) if isinstance(r, dict)]
        agg = template.get("aggregator") or {}
        calls: list[tuple[str, str]] = []
        unknown = False
        if kind == "roundtable":
            for role in roles:
                provider = str(role.get("provider") or "").strip()
                if not provider:
                    unknown = True
                for _ in range(1 + max(0, int(debate_rounds))):
                    calls.append((provider, str(role.get("prompt") or "")))
            agg_provider = str(agg.get("provider") or "").strip()
            if not agg_provider:
                # 收口缺省 = 当前主模型（会话级），模板本身没有声明，按不可估价
                unknown = True
            calls.append((agg_provider, str(agg.get("prompt") or "")))
        elif kind == "adversarial":
            seats = [r for r in roles[:3]]
            for role in seats:
                provider = str(role.get("provider") or "").strip()
                if not provider:
                    unknown = True
                calls.append((provider, str(role.get("prompt") or "")))
            # 不足三角色的空位由自动服务池补位（池里的服务名是已知的）
            pool = self._preview_auto_pool()
            for i in range(len(seats), 3):
                if i - len(seats) < len(pool):
                    calls.append((pool[i - len(seats)][0], ""))
                else:
                    unknown = True
            agg_provider = str(agg.get("provider") or "").strip()
            if not agg_provider:
                unknown = True
            calls.append((agg_provider, str(agg.get("prompt") or "")))
        else:
            # 团队：per-role 的 prompt token 按轮转分摊到 max_calls 次调用；
            # 用户总管（aggregator 为空）不产生额外调用，不算 unknown。
            if estimated_calls > 0 and roles:
                for i in range(estimated_calls):
                    role = roles[i % len(roles)]
                    provider = str(role.get("provider") or "").strip()
                    if not provider:
                        unknown = True
                    calls.append((provider, str(role.get("prompt") or "")))
            elif estimated_calls > 0:
                unknown = True
        return calls, unknown

    @staticmethod
    def _preview_price_is_configured(cfg) -> bool:
        pz = cfg.usage_pricing
        return bool(
            pz.price_in or pz.price_out or pz.price_cache
            or any(
                e.price_in or e.price_out or e.price_cache
                for e in pz.providers.values()
            )
        )

    def _preview_price_for(self, provider: str) -> tuple[float, float, float]:
        pz = self.cfg.usage_pricing
        entry = pz.providers.get(provider or "")
        if entry is not None:
            return entry.price_in, entry.price_out, entry.price_cache
        return pz.price_in, pz.price_out, pz.price_cache

    def collab_template_preview(self, params: dict) -> dict:
        """collab.preview：跑前清单（角色/工具/命令前缀/预算/粗估费用）。

        费用只用 config.toml 的 [usage_pricing] 真实价目折算；任一角色没有
        声明服务（或收口角色缺省当前主模型）时如实返回 unknown_provider，
        绝不编一个 0 元；团队 max_calls=0（不限）时返回 unbounded。
        """
        name = str(params.get("name") or "").strip()
        source = str(params.get("source") or "").strip() or None
        if not name:
            raise CollabTemplateError("找不到协作模板：")
        template = self._find_collab_template(name, source)
        if template is None:
            if source:
                raise CollabTemplateError("模板来源不匹配")
            raise CollabTemplateError(f"找不到协作模板：{name}")
        kind = str(template.get("kind") or "")
        roles = [r for r in (template.get("roles") or []) if isinstance(r, dict)]
        limits = template.get("limits") or {}
        budget = template.get("budget") or {}
        needs = derive_needs(roles)

        roles_preview: list[dict] = []
        pool = self._preview_auto_pool()
        for role in roles:
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            if provider_name:
                pc = self.cfg.providers.get(provider_name)
                if pc is None:
                    resolved, build_error = False, "未配置该服务"
                elif resolve_api_key(provider_name, pc) is None:
                    resolved, build_error = False, "该服务未配置 API Key"
                else:
                    resolved, build_error = True, ""
                    if not model:
                        model = pc.model or ""
            else:
                resolved = bool(pool)
                build_error = "" if pool else "没有已配置 Key 的服务"
            roles_preview.append({
                "key": str(role.get("key") or ""),
                "name": str(role.get("name") or ""),
                "provider": provider_name,
                "model": model,
                "needs": str(role.get("needs") or "read"),
                "resolved": resolved,
                "build_error": build_error,
            })

        tools, command_prefixes, always_confirm = self._preview_tool_entries(
            template, needs
        )

        if kind == "team":
            effective_count, dropped = self._preview_team_members(roles)
        elif kind == "adversarial":
            effective_count = min(3, len(roles))
            dropped = [
                str(r.get("name") or "") for r in roles[3:]
            ]
        else:
            effective_count, dropped = len(roles), []

        debate = 0
        if kind == "roundtable":
            debate = int(
                limits.get("debate_rounds") or self.cfg.roundtable.debate_rounds or 0
            )
            debate = max(0, min(debate, 2))
            estimated_calls = len(roles) * (1 + debate) + 1
        elif kind == "adversarial":
            estimated_calls = min(3, len(roles)) + 1
        else:
            estimated_calls = int(budget.get("max_calls") or 0)

        calls, unknown_provider = self._preview_pricing_calls(
            template, estimated_calls, debate,
        )
        estimated_input = sum(
            PREVIEW_INPUT_FLOOR_TOKENS + estimate_text_tokens(prompt or "")
            for _provider, prompt in calls
        )
        estimated_output = PREVIEW_OUTPUT_TOKENS * len(calls)

        if unknown_provider:
            cost_reason, estimated_cost, priced = "unknown_provider", None, False
        elif not self._preview_price_is_configured(self.cfg):
            cost_reason, estimated_cost, priced = "no_price", None, False
        elif estimated_calls <= 0:
            # 团队 max_calls=0 = 不限：调用数未知，不编一个 0 元
            cost_reason, estimated_cost, priced = "unbounded", None, False
        else:
            cost = 0.0
            for provider_name, prompt in calls:
                cost += self._fold_usage_cost(
                    PREVIEW_INPUT_FLOOR_TOKENS + estimate_text_tokens(prompt or ""),
                    PREVIEW_OUTPUT_TOKENS, 0,
                    self._preview_price_for(provider_name),
                )
            cost_reason, estimated_cost, priced = "ok", round(cost, 4), True

        builtin = bool(template.get("builtin"))
        return {
            "name": str(template.get("name") or ""),
            "kind": kind,
            "needs": needs,
            "source": str(template.get("source") or ""),
            "desc": str(template.get("desc") or ""),
            "roles": roles_preview,
            "roles_declared": len(roles),
            "effective_member_count": effective_count,
            "dropped_roles": dropped,
            "tools": tools,
            "command_prefixes": command_prefixes,
            "always_confirm": always_confirm,
            # 本期团队是团队级工具并集（team.py 每个成员共用同一 registry），
            # 角色级 tools 只用于声明与预览；不宣称逐成员隔离
            "role_scoped": False,
            "role_scope_note": (
                "工具按团队声明的并集生效；角色级 tools 用于声明与预览，"
                "本期不做逐成员隔离"
            ),
            "max_calls": int(budget.get("max_calls") or 0),
            "max_tokens": int(budget.get("max_tokens") or 0),
            "max_seconds": int(budget.get("max_seconds") or 0),
            "estimated_calls": estimated_calls,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "priced": priced,
            "estimated_cost": estimated_cost,
            "currency": "CNY",
            "cost_reason": cost_reason,
            "cost_basis": (
                "按每次调用输入≤角色提示词+400 tokens、输出 800 tokens 粗估；"
                "价目取 config.toml [usage_pricing]"
            ),
            # 内置团队模板只在代码里、不落盘：无法常驻/定时复用
            "can_resident": bool(
                kind == "team" and template.get("source") == "team" and not builtin
            ),
        }
