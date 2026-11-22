"""工作区信任：项目级配置在获信任前不得自动生效（trust.* 全族）。

D6 基础设施与迁移域（backend.py Mixin 拆分批 2 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

`trust` 属性（懒建 WorkspaceTrust）与六个 trust.* WS 方法在此；
`recheck_trust_before_turn` 在每轮对话起点被 _run_turn_pipeline 调用
（审查 P2-4，轮首热路径），行为逐点不变。`trust_grant/revoke/revoke_path`
的收尾都走 _sync_mcp_changes + _reload_project_skills 同一套差量收口。

跨域只读（不收敛，注记）：`self._sync_mcp_changes`（McpMixin，D6 同域）、
`self.skills`（内核装配的 SkillLoader）、`self.mcp_warnings`（D6 McpMixin
域内状态）、`self._refresh_system_prompts()`（内核）——经共享 self 取用。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ...config import skysheep_home
from ...security.trust import STATE_PENDING, WorkspaceTrust, list_trusted, revoke_by_path

logger = logging.getLogger(__name__)


class TrustMixin:
    """workspace trust 门族（trust 属性 + trust.* WS 方法 + 轮首重验）。方法自 backend.py 逐字搬入。"""

    # ---- workspace trust：项目级配置在获信任前不得自动生效 ----

    @property
    def trust(self) -> WorkspaceTrust:
        """当前项目的信任状态（按项目路径记忆在用户主目录）。"""
        if self._trust is None:
            self._trust = WorkspaceTrust(skysheep_home(), self.working_dir)
        return self._trust

    def _project_mcp_path_if_trusted(self) -> Path | None:
        """未获信任时返回 None：项目级 MCP 配置不得在本轮启动时被读取/拉起。"""
        if self.working_dir is None or not self.trust.is_trusted():
            return None
        return self._mcp_project_path()

    def _project_skills_dir_if_trusted(self) -> Path | None:
        """未获信任时返回 None：项目级技能不得自动发现并注入系统提示词。"""
        if self.working_dir is None or not self.trust.is_trusted():
            return None
        return self.working_dir / ".skysheep" / "skills"

    def _mcp_reconnect_gate(self, name: str) -> bool:
        """自动重连前的放行门（注入 MCPManager，审查 P2-5）。

        显式 trust.revoke 走 _sync_mcp_changes 的 forget 路径，断线任务会被
        取消/查无配置，不经过这里；这里防的是另一条缝：会话运行期间项目配置
        被外部改动导致信任跌成 pending（尚未触发任何配置同步）时，断线的
        项目级服务器仍会按 manager 内存里的旧配置自动重连。按名字判断它是否
        来自项目级 mcp.json，是则要求信任仍然有效；全局配置的服务器不受影响。
        """
        path = self._mcp_project_path()
        if path is None or not path.is_file():
            return True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = (data or {}).get("mcpServers") or {}
        except (OSError, ValueError):
            return True
        if name not in servers:
            return True
        return self._project_mcp_path_if_trusted() is not None

    async def trust_status(self) -> dict:
        """当前项目的信任状态（给前端渲染确认横幅）。"""
        return self.trust.state()

    def trust_list(self) -> dict:
        """全部已信任项目的清单（设置页 · 安全与后台 管理用）。

        记录里是归一化路径（Windows 上小写），当前项目单独标出来；
        含绝对路径，与 session.backups 同理只给本机界面。
        """
        current = str(self.working_dir) if self.working_dir else ""
        items = list_trusted(skysheep_home())
        for e in items:
            e["is_current"] = bool(current) and os.path.normcase(current) == e["path"]
        return {"items": items}

    async def trust_revoke_path(self, path: str) -> dict:
        """按路径撤销信任（收紧动作，远端也允许）。

        撤的是「当前项目」时，还要断开项目级 MCP、重载技能——与 trust.revoke
        同一套收尾；撤别的项目只删记录（它没在跑，无需断开任何东西）。
        """
        root = str(path or "").strip()
        if not root:
            raise RuntimeError("缺少项目路径")
        removed = revoke_by_path(skysheep_home(), root)
        if not removed:
            raise RuntimeError("该路径不在已信任清单里")
        is_current = (
            self.working_dir is not None
            and os.path.normcase(str(self.working_dir)) == os.path.normcase(root)
        )
        if is_current:
            self._trust = None  # 失效缓存，下次访问按已撤销重算
            state = self.trust.state()
            await self._sync_mcp_changes()
            self._reload_project_skills()
            return {**state, "revoked": root}
        return {"revoked": root}

    async def trust_grant(self) -> dict:
        """用户确认信任本项目：记录指纹，并把项目级 MCP/技能接上（无需重启）。"""
        state = self.trust.grant()
        await self._sync_mcp_changes()
        self._reload_project_skills()
        return {**state, "mcp_warnings": list(self.mcp_warnings)}

    async def trust_revoke(self) -> dict:
        """取消信任：断开项目级 MCP 服务器，并重新发现技能（项目级不再生效）。"""
        state = self.trust.revoke()
        await self._sync_mcp_changes()
        self._reload_project_skills()
        return {**state, "mcp_warnings": list(self.mcp_warnings)}

    def _reload_project_skills(self) -> None:
        """按当前信任状态重新发现技能，并把系统提示词刷到所有 agent。"""
        self.skills.project_dir = self._project_skills_dir_if_trusted()
        self.skills.discover()
        self._refresh_system_prompts()

    async def recheck_trust_before_turn(self) -> None:
        """轮次起点重验工作区信任（审查 P2-4）。

        信任只在启动/切项目/信任操作这些「入口」被求值：会话运行期间项目级
        配置被外部改动（git pull、被注入进程写入）后，已连的项目级 MCP 会
        保持连接、load_skill 也照读磁盘上的新正文——引擎自身没有重查点。
        这里趁每轮对话开始补一次：`state()=="pending" 且 changed=True` 恰好
        表达「曾经信任过、现在配置变了」，此时走与 trust.revoke 同一套差量
        收口（断项目级 MCP、重发现技能），只影响当前项目。
        """
        if self.working_dir is None:
            return
        state = self.trust.state()
        if state.get("state") != STATE_PENDING or not state.get("changed"):
            return
        logger.warning(
            "工作区信任在会话运行期间失效（项目级配置被外部改动），收口项目级 MCP 与技能：%s",
            self.working_dir,
        )
        self._trust = None  # 失效缓存，后续访问按 pending 重算
        await self._sync_mcp_changes()
        self._reload_project_skills()
