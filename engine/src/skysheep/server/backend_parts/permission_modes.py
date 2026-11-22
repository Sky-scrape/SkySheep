"""分级权限模式（confirm / accept_edits / full_access）与按会话档位覆盖。

D1 会话与运行时域（backend.py Mixin 拆分批 5b 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

安全口径（不许放松）：
- 放宽档只由本机用户切换（session.accept_get / session.accept_set 在 WS 层是
  local_only；permission.set_mode 由 local_gate 拦下「自动编辑 / 完全访问」，
  远端连接只能把档位设回 confirm——见 server/_dispatch_session.py 与
  _dispatch_chat.py 的 _gate_permission_set_mode）；
- 调档记日志（~/.skysheep/logs/desktop.log 可追溯）；
- 渠道门跳过：渠道姿态只由渠道门决定，桌面侧档位不写进无人值守通道
  （design-gate-sync §3.6 加固；_sync_default_accept_to_runtimes 与
  session_accept_set 同口径）。

字段所有权（D1，本批只注记不搬）：`_session_accepts`（会话级档位覆盖，
sid → 0/1/2）由主类 __init__ 一次铺齐；内核 `_runtime_gate` 建私有门时
重放本节覆盖，删除会话由 LifecycleMixin 清理。本 Mixin 无 __init__、
无类级可变属性。
"""

from __future__ import annotations

import logging

from ...channels import ChannelGate

logger = logging.getLogger("skysheep.security")


class PermissionModesMixin:
    """分级权限模式与按会话档位覆盖（D1 批 5b 外迁件）。

    方法自 backend.py 逐字搬入；状态全部来自主类共享 self（无 __init__）。
    """

    # ---- 分级权限模式（对标 Codex / Claude Code：confirm / accept_edits / full_access） ----

    def permission_mode(self) -> str:
        if self.gate is None:
            return "confirm"
        if self.gate.auto_accept_all:
            return "full_access"
        return "accept_edits" if self.gate.auto_accept_write else "confirm"

    def _apply_gate_accept_pref(self, value) -> None:
        """把 ui.json 的 accept_edits（0=安全执行 1=自动编辑 2=完全访问）应用到门控。

        启动恢复 / 换项目 / 导入设置包三处共用；脏值一律落回安全执行。
        """
        if self.gate is None:
            return
        v = value if value in (0, 1, 2) else 0
        self.gate.auto_accept_write = v in (1, 2)
        self.gate.auto_accept_all = v == 2
        self._sync_default_accept_to_runtimes()

    def _sync_default_accept_to_runtimes(self) -> None:
        """引擎默认档变化后，同步到「没有会话级覆盖」的 runtime 私有门。

        默认档（permission.set_mode / ui.json 的 accept_edits）是所有未覆盖
        会话的当前值；只有 session.accept_set 显式覆盖过的会话才脱离默认
        （那正是「按会话独立」的语义）。runtime 的门在 _runtime_gate 里是
        私有副本，不随 self.gate 原地变，所以默认档变化要在这里显式追平。

        渠道门跳过（design-gate-sync §3.6 加固）：渠道姿态只由渠道门决定，
        authorize 不读档位（无人值守口径），档位本就不该出现在一个不消费
        它的门上——写上去只会让继承的批预检谓词读到与渠道 authorize 分歧
        的档位（分歧 D2）。"""
        for sid, rt in self.runtimes.items():
            if sid in self._session_accepts:
                continue
            g = rt.agent.gate
            if (
                g is not None
                and g is not self.gate
                and not isinstance(g, ChannelGate)
            ):
                g.auto_accept_write = self.gate.auto_accept_write
                g.auto_accept_all = self.gate.auto_accept_all

    async def set_permission_mode(self, mode: str) -> dict:
        """confirm = 安全执行，写入/命令都确认（默认）；accept_edits = 自动编辑，
        工作目录内写入自动放行、命令仍确认；full_access = 完全访问，写入与命令
        都不再征询。

        档位存在 ui.json，下次启动沿用；放宽档只由本机用户在界面上切换（远端
        调用在 server/app.py 的 dispatch 层拦下，需测试或脚本驱动时直接调用本方法）。
        切换记一条日志：这是降低防护的动作，事后能从 ~/.skysheep/logs/desktop.log 追溯。
        """
        if mode not in ("confirm", "accept_edits", "full_access"):
            raise RuntimeError("权限模式只支持 confirm / accept_edits / full_access")
        if self.gate is None:
            raise RuntimeError("引擎尚未就绪")
        self.gate.auto_accept_write = mode in ("accept_edits", "full_access")
        self.gate.auto_accept_all = mode == "full_access"
        await self.save_ui_prefs(
            {"accept_edits": {"confirm": 0, "accept_edits": 1, "full_access": 2}[mode]}
        )
        logger.info(
            "权限模式切换为 %s（自动允许写入=%s，完全访问=%s，工作目录：%s）",
            mode,
            self.gate.auto_accept_write,
            self.gate.auto_accept_all,
            self.working_dir,
        )
        self._sync_default_accept_to_runtimes()
        return {"mode": self.permission_mode()}

    def _accept_mode_of(self, session_id: str) -> str:
        """会话当前生效的权限三档：会话级覆盖优先，未覆盖回引擎默认。"""
        saved = self._session_accepts.get(session_id)
        if saved in (0, 1, 2):
            return {0: "confirm", 1: "accept_edits", 2: "full_access"}[saved]
        return self.permission_mode()

    async def session_accept_get(self, session_id: str) -> dict:
        """会话当前生效的权限三档（主栏/分屏盾牌按面显示用）。

        会话级覆盖（session.accept_set）优先；未覆盖回引擎默认档
        （ui.json 的 accept_edits）。本机专属（local_only）——降防护的
        开关不向远程开放，与 permission.set_mode 同一面。"""
        await self._get_owned_session(session_id, local=True)
        return {
            "session_id": session_id,
            "mode": self._accept_mode_of(session_id),
            "overridden": session_id in self._session_accepts,
            "engine_mode": self.permission_mode(),
        }

    async def session_accept_set(self, session_id: str, mode: str) -> dict:
        """按会话设置权限三档：只影响该会话 runtime 的门，不动引擎默认——
        别的会话（含主栏）保持自己的档位。runtime 已建则热调其私有门；
        还没建只记覆盖，_runtime_gate 在建门时重放。会话删除随清理，
        引擎默认档（permission.set_mode）仍是新会话的起点。

        本机专属（local_only）：放宽档只由本机用户切换（安全审查根因一，
        与 permission.set_mode 同一收口）；调档记日志，事后可追溯。

        渠道门跳过（design-gate-sync §3.6 加固，与 _sync_default_accept_to_
        runtimes 同口径）：渠道会话的授权面只由渠道门决定，桌面侧给渠道
        会话调档不得把引擎档带到无人值守通道的渠道门上。"""
        if mode not in ("confirm", "accept_edits", "full_access"):
            raise RuntimeError("权限模式只支持 confirm / accept_edits / full_access")
        await self._get_owned_session(session_id, local=True)
        v = {"confirm": 0, "accept_edits": 1, "full_access": 2}[mode]
        self._session_accepts[session_id] = v
        rt = self.runtimes.get(session_id)
        if rt is not None and rt.agent.gate is not None \
                and not isinstance(rt.agent.gate, ChannelGate):
            rt.agent.gate.auto_accept_write = v in (1, 2)
            rt.agent.gate.auto_accept_all = v == 2
        logger.info(
            "会话 %s 权限档位切换为 %s（自动允许写入=%s，完全访问=%s，工作目录：%s）",
            session_id, mode, v in (1, 2), v == 2, self.working_dir,
        )
        return {"session_id": session_id, "mode": mode}
