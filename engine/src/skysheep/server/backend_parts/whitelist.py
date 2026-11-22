"""项目白名单规则（设置页）：CRUD、失效标记、规则测试器、导入导出。

D6 基础设施与迁移域（backend.py Mixin 拆分批 2 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

stale 失效标记与 gate 匹配侧 fail-closed 同源（复用 security.gate 的
_is_arbitrary_exec_prefix 与 RULE_KINDS，不造第二套判定）；手动添加与
导入共用同一校验器；回传 rules 的读路径统一走 _enriched_rules，字段集一致。

跨域只读（不收敛，注记）：`self.store` / `self.gate` / `self.project` /
`self._require_project`（内核会话与权限面）——经共享 self 按 MRO 取用。
"""

from __future__ import annotations

import time

from ...security.gate import _RUN_COMMAND, RULE_KINDS, _is_arbitrary_exec_prefix


class WhitelistMixin:
    """白名单规则 CRUD 与校验（whitelist.* WS 方法的实现面）。方法自 backend.py 逐字搬入。"""

    # ---- 白名单（设置页）：手动添加 / 清空 / 测试 / 导入导出 ----

    async def list_whitelist_rules(self) -> list[dict]:
        """项目白名单规则列表（whitelist.list），每条带 stale 失效标记。

        stale 判定与 gate 匹配侧 fail-closed 同源，不造第二套判定：
        2.3.0 收紧后 docker/ssh/scp 两词前缀规则不再命中（_is_arbitrary_exec_prefix）；
        更早的审查 S-12 起 delete_file 整工具 always 规则也不再认（gate 的
        _matches_core）——列表把这类「留着也不会再放行」的规则亮出来，提示清理。
        """
        return await self._enriched_rules()

    async def _enriched_rules(self) -> list[dict]:
        """规则列表回读的唯一出口：凡回传 rules 的读路径（whitelist.list /
        whitelist.add / whitelist.enable / whitelist.import）都走这里，
        字段集才一致——add 回传同条规则与随后 list 完全相同。
        """
        if self.project is None:
            return []
        rules = await self.store.list_rules(self.project.id)
        return [self._stale_fields(r) for r in rules]

    @staticmethod
    def _stale_fields(rule: dict) -> dict:
        """就地补 stale / stale_reason 字段（设置页失效徽章的数据源）。"""
        stale = (
            rule.get("tool") == _RUN_COMMAND
            and rule.get("kind") == "prefix"
            and _is_arbitrary_exec_prefix(rule.get("pattern") or "")
        )
        reason = "安全收紧（2.3.0）：该类前缀已回落逐次确认，规则不再命中" if stale else ""
        if not stale and rule.get("tool") == "delete_file" and rule.get("kind") == "always":
            # 与 gate 匹配侧同口径（gate._matches_core 对 delete_file 的整工具
            # 规则一律不认，审查 S-12）：这类存量死规则留着也不会再放行，
            # 亮出来提示清理——放行删除面请按具体路径固化 exact 规则
            stale = True
            reason = ("安全收紧（审查 S-12）：delete_file 不再整工具放行，规则不再命中；"
                      "请按具体路径固化 exact 规则")
        rule["stale"] = stale
        rule["stale_reason"] = reason
        return rule

    @staticmethod
    def _validate_whitelist_rule(tool: str, kind: str, pattern: str) -> tuple[str, str, str]:
        """手动添加 / 导入共用的规则校验，返回规范化后的 (tool, kind, pattern)。"""
        tool = (tool or "").strip()
        pattern = (pattern or "").strip()
        if not tool:
            raise RuntimeError("工具名不能为空")
        if kind not in RULE_KINDS:
            raise RuntimeError("规则类型不合法")
        if kind == "always":
            pattern = ""  # always 不携带参数
        elif not pattern:
            raise RuntimeError("该类型需要填写匹配内容")
        if len(pattern) > 500:
            raise RuntimeError("匹配内容过长（上限 500 字符）")
        return tool, kind, pattern

    async def add_whitelist_rule(self, tool: str, kind: str, pattern: str = "") -> dict:
        """手动添加一条项目级规则（设置页入口）。写库后立即 reload，与 remove 同一路径。"""
        self._require_project("保存白名单规则")  # 白名单按项目持久化
        tool, kind, pattern = self._validate_whitelist_rule(tool, kind, pattern)
        rules = await self.store.list_rules(self.project.id)
        if any(
            r["tool"] == tool and r["kind"] == kind and r["pattern"] == pattern
            for r in rules
        ):
            raise RuntimeError("已存在完全相同的规则")
        await self.store.add_rule(self.project.id, tool, kind, pattern)
        await self.gate.load_project_rules()
        # 回传与 whitelist.list 同一富化：stale 标记随写即回，字段集两处一致
        return {"rules": await self._enriched_rules()}

    async def clear_whitelist_rules(self, kind: str = "") -> dict:
        """清空项目白名单（kind 为空 = 全部；收紧动作，远端也放行）。

        无项目态没有可清的白名单：直接返回 0（收紧动作不报错，远端也放行）。"""
        if self.project is None:
            return {"removed": 0}
        removed = await self.store.clear_rules(
            self.project.id, kind if kind in RULE_KINDS else ""
        )
        await self.gate.load_project_rules()
        return {"removed": removed}

    async def remove_whitelist_rule(self, rule_id: int) -> dict:
        """删一条项目级规则（设置页入口）。带归属校验：凭枚举到的 rule_id
        不能删其他项目的规则（安全审查 B11），store 层把 project_id 写进 DELETE。
        """
        self._require_project("删除白名单规则")  # 白名单按项目持久化
        rules = await self.store.list_rules(self.project.id)
        if not any(r["id"] == rule_id for r in rules):
            raise RuntimeError("规则不存在，可能已被删除")
        await self.store.remove_rule(rule_id, project_id=self.project.id)
        await self.gate.load_project_rules()
        return {"removed": rule_id}

    async def set_whitelist_rule_enabled(self, rule_id: int, enabled: bool) -> dict:
        """启停一条项目级规则（设置页开关）：临时停用不必删配置。

        停用是收紧（远端也允许）；启用是放宽，与 whitelist.add 同姿态，
        由 app.py 限本机调用。归属校验在 store 层（同 remove_rule）。
        """
        self._require_project("修改白名单规则")
        await self.store.set_rule_enabled(rule_id, self.project.id, bool(enabled))
        await self.gate.load_project_rules()
        return {"id": rule_id, "enabled": bool(enabled),
                "rules": await self._enriched_rules()}

    async def check_whitelist_rule(self, tool: str, text: str) -> dict:
        """规则测试器：当前规则会让这条调用直接放行、还是弹确认。

        解释前先从库里重载一遍项目规则：聊天权限卡「总是允许」走会话私有
        gate 的 persist_rule（写库 + 只更新私有实例内存），主 gate 的规则
        缓存不随那条路径刷新——不重载的话测试器会对实际已放行的规则报
        「没有命中」，与聊天里的真实行为相反。
        """
        tool = (tool or "").strip()
        if not tool:
            raise RuntimeError("工具名不能为空")
        await self.gate.load_project_rules()
        return self.gate.explain(tool, text or "")

    async def export_whitelist(self) -> dict:
        self._require_project("导出白名单")  # 白名单按项目持久化
        rules = await self.store.list_rules(self.project.id)
        return {
            "version": 1,
            "project": self.project.name,
            "exported_at": time.time(),
            "rules": [
                {"tool": r["tool"], "kind": r["kind"], "pattern": r["pattern"]}
                for r in rules
            ],
        }

    async def import_whitelist(self, rules: list) -> dict:
        """导入规则（合并模式）：逐条走与手动添加相同的校验，重复或不合法的跳过。"""
        self._require_project("导入白名单规则")  # 白名单按项目持久化
        items = rules if isinstance(rules, list) else []
        existing = await self.store.list_rules(self.project.id)
        seen = {(r["tool"], r["kind"], r["pattern"]) for r in existing}
        added = skipped = 0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            try:
                tool, kind, pattern = self._validate_whitelist_rule(
                    str(item.get("tool", "")),
                    str(item.get("kind", "")),
                    str(item.get("pattern", "") or ""),
                )
            except RuntimeError:
                skipped += 1
                continue
            if (tool, kind, pattern) in seen:
                skipped += 1
                continue
            await self.store.add_rule(self.project.id, tool, kind, pattern)
            seen.add((tool, kind, pattern))
            added += 1
        await self.gate.load_project_rules()
        return {
            "added": added,
            "skipped": skipped,
            "rules": await self._enriched_rules(),
        }
