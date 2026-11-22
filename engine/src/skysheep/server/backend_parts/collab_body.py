"""D2 多模型协作域：成员装配 / 圆桌与对抗 / 团队主体（backend.py Mixin 拆分批 6 外迁件）。

从 backend.py 的「圆桌 → 圆桌设置 → 对抗 → 对抗设置 → 团队 → 团队设置 →
团队模板 → 常驻团队 → 频道回放」连续职责段整段搬入（49 个方法，设计 §4.1
批 6），方法体逐字保留，逻辑不变（与本文件同批新增的五个跨域入口见下，
属新增小件，不是改逻辑）。

三段职责：
- 成员装配：_build_member_provider / _auto_pool_rank / _auto_pool_services /
  _resolve_members（圆桌与对抗共用的成员 Provider 与「自动池」选服务，坏
  服务降权见 _BAD_POOL_CATEGORIES）；
- 圆桌与对抗：roundtable.get/save 与 adversarial.get/save 的设置读写、
  _adversarial_body 四角色流水线（发现→调查→建议→裁判）、adversarial.*
  的导出 / 修复提示 / 历史对比；
- 团队：建队（_team_create，含模板实例化 / 预算 / 工具裁剪）、一轮团队轮
  （_team_body，用户总管与 AI 总管两条路径）、工单板与频道、团队设置与
  模板、常驻团队登记、team.log 频道回放。

模块级 helper（_TEAM_INTERRUPTED_KEEP / _ADV_* 标签表 / _adversarial_fingerprints /
_adversarial_report_markdown / TEAM_DIGEST_EXCERPT_CHARS / _team_digest /
_ROOM_TASK_STATUSES / _room_empty_stats / _room_stats）只服务本域，随迁；
其中 _adversarial_fingerprints / _adversarial_report_markdown 被
tests/test_adversarial.py 从 skysheep.server.backend 直接 import，backend.py
只在 import 行转发同一对象（不复制实现）。_PERSIST_HARVEST_TIMEOUT 是内核
常量（批 5a 起供 _run_turn_pipeline 用），不随迁：由 backend.py 在模块末尾
单向注入本模块（与 chat_pipeline 同款接线）。

本 Mixin 无 __init__、无类级可变属性（_BAD_POOL_CATEGORIES 是不可变
frozenset）：状态仍由主类 ServerBackend.__init__ 一次铺齐。

字段所有权（D2）：_teams（sid → TeamOrchestrator 的活动团队登记，内存态、
不跨进程）、_teams_interrupted（已中断记录，按 _TEAM_INTERRUPTED_KEEP 截断）、
_team_state_path（team_active.json 落点，测试可注入）——三者在本文件内裸
访问。resident_teams（常驻团队注册表）由主类 __init__ 构造、内核 setup 调
load()，本域只经 store API 读登记（team.get / team.room 的 resident 标志）
与写登记（team_resident_set / team_resident_clear），不重赋字段。

跨域收敛（批 6，设计 §3.3 雷区 _teams）：15 处跨域调用点（2 写 + 13 读：
成员判定 4 / 键式取 3 / 遍历 6）全部改走本 Mixin 的入口——
- 三个只读 accessor（按形态对号，缺一则某个读点没处落）：
  team_active(sid) ≡ `sid in self._teams`（chat_pipeline 的 4 处成员判定）；
  team_orchestrator(sid) ≡ `.get(sid)`（channels._sync_team_gate 与 automation
  的两处 resident live 键式取）；
  team_orchestrators() 返回 **dict 本体** `getattr(self, "_teams", {})`，不是
  values 视图——values 遍历照常 `.values()`，迁移判忙需要键故 `.items()`
  （migration.py 的判忙），沿既有 getattr 防御写法。
- 两个写封装（各自与原块逐字等价）：
  team_detach_for_delete(sid)：删除会话摘队（pop 编排器 → pop 中断记录 →
  条件 persist），返回被摘的编排器（delete_session 等在飞轮收尾之后才释放
  它的 provider）；
  team_interrupt_for_project_switch()：切项目中断（if self._teams 起、按
  runtimes[sid].ctx.own 过滤出随主栏失效的会话 → pop → 释放 provider →
  登记 interrupt_reason="项目已切换，团队已中断" → 条件 persist）。

第 16 处读点（server/_dispatch_chat.py 的 _h_permission_queue）在分发层，超出
设计 §3.3 的 ≤15 闸门，按降级条款保留裸读 + 注记，本批不动。读 D1/D3 的状态
按既有注记：_get_owned_session（D1 归属校验）、runtimes 的 ctx.own /
project_id（内核池）、_session_effective_model 与 _release_turn_providers（D1
SessionModelMixin）、_check_budget_alerts（D3 AutomationMixin，预算告警）、
_preload_mentioned_files（D2 peer RoundtableBodyMixin）、_collab_template_warnings
（D2 peer CollabGalleryMixin）、ws_emitters（内核，只遍历 emit）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ...bgtasks import spawn_bg
from ...config import ConfigError, load_config, resolve_api_key, update_config_section
from ...core import Agent
from ...core.adversarial import (
    PHASE_ADVISOR,
    PHASE_FINDER,
    PHASE_INVESTIGATOR,
    PHASE_JUDGE,
    AdversarialOutcome,
    run_adversarial,
)
from ...core.adversarial import findings_meta as adversarial_findings_meta
from ...core.adversarial import stats as adversarial_stats
from ...core.adversarial import usage_rows as adversarial_usage_rows
from ...core.collab_budget import BudgetedProvider
from ...core.collab_templates import (
    CollabTemplate,
    RunBudget,
    TeamCfgView,
    collect_command_prefixes,
    derive_needs,
    effective_tool_entries,
    is_builtin_template_name,
    validate_collab_template,
)
from ...core.context import compact_history
from ...core.roundtable import MEMBER_ROLES, MemberSpec
from ...core.team import (
    ChannelMessage,
    DirectorSpec,
    TeamError,
    TeamMemberSpec,
    TeamMessageSink,
    TeamOrchestrator,
    TeamTemplateDirector,
    TeamTemplateMember,
    validate_team_template_name,
)
from ...core.team_resident import ResidentTeamEntry
from ...core.team_store import TEAM_TEMPLATE_PERSONA_MAX
from ...events import AssistantMessage, NoticeEvent, TurnFinished, TurnStarted
from ...messages import ImageBlock, Message, TextBlock
from ...models import Provider
from ...models.factory import build_provider
from ...obs import warning as obs_warning
from ...security.gate import _RUN_COMMAND, GrantedGate, PermissionGate
from ...textio import read_text_file, write_text_atomic
from ...tools import ChangeRecorder, Safety, ToolRegistry
from ._shared import EmitFn, _dump_emit, _strip_image_blocks
from .collab_team_templates import _CollabTeamTemplate, _CollabTeamTemplateBudget

if TYPE_CHECKING:
    # 循环引用的类型侧声明：backend_parts 不得运行期 import backend。该常量的
    # 定义在 backend.py（内核单一事实来源，批 5a 起同时供 _run_turn_pipeline），
    # 运行期由 backend 在模块末尾单向注入本模块命名空间（见 backend.py
    # 「内核共用件接线」注记）——此处只让静态检查看到绑定。
    from ..backend import _PERSIST_HARVEST_TIMEOUT


# ---- 模块级 helper（逐字搬自 backend.py）----
# team_active.json 里「已中断」记录的保留上限：团队运行时本就不跨进程恢复，
# 这些记录只用于历史里的只读频道回放（凭 team_id 拉全量），按最近保留即可。
_TEAM_INTERRUPTED_KEEP = 50


_ADV_VERDICT_LABELS = {"confirmed": "成立", "partial": "部分成立", "refuted": "推翻", "pending": "待定"}
# 严重度四档与流水线的合法值集（core/adversarial.py _VALID_SEVERITIES）及前端
# ADV_SEVERITY_LABEL（static/app.js）对齐：致命级在导出文件里渲染中文。
# 旧表里的 "info" 键移除：流水线从产出过 severity 起（git 追溯到对抗功能首个
# 提交）白名单就只有 critical/high/medium/low，落库前非法值已归为 medium，
# "info" 是无来源的死键——渲染对未知值本就回退原文（.get 第二参），删掉与
# 前端行为完全一致。
_ADV_SEV_LABELS = {"critical": "致命", "high": "高", "medium": "中", "low": "低"}
_ADV_ROLE_LABELS = {"finder": "发现者", "investigator": "调查者", "advisor": "建议者", "judge": "裁判"}


def _adversarial_fingerprints(meta: dict) -> dict:
    """对抗结果的「指纹视图」：计数 + 每条发现的稳定键（P3 历史对比用）。

    键取「位置 + 描述首行」压空白后的文本，不含 id/序号：两次审查是两次独立
    流水线，id 不跨轮稳定，而同一个 bug 在两轮里位置与描述首行几乎一样——
    这正是「上次那个问题还在不在」要回答的问题。认不出来的（无位置无描述）
    退化成整段描述的前 80 字，宁可对不上也不错配。
    """
    meta = meta if isinstance(meta, dict) else {}
    stats = meta.get("stats") or {}
    findings = [f for f in (meta.get("findings") or []) if isinstance(f, dict)]
    keys: set[str] = set()
    titles: dict[str, str] = {}
    for f in findings:
        raw_desc = str(f.get("description") or "").strip()
        first_line = raw_desc.splitlines()[0] if raw_desc else ""
        title = " ".join(first_line.split())[:80] or str(f.get("id") or "（无描述）")
        location = " ".join(str(f.get("location") or "").split())
        key = (location + "｜" + title).lower()
        keys.add(key)
        titles[key] = title
    summary = {
        "total": int(stats.get("total") or len(findings)),
        "confirmed": int(stats.get("confirmed") or 0),
        "partial": int(stats.get("partial") or 0),
        "refuted": int(stats.get("refuted") or 0),
        "pending": int(stats.get("pending") or 0),
    }
    return {"summary": summary, "keys": keys, "titles": titles}


def _adversarial_report_markdown(session_title: str, meta: dict, seq: int) -> tuple[str, str]:
    """把落库的对抗 meta 组装成结构化 Markdown 报告（P3 导出）。

    结构：报告头（来源/裁判/角色名册/裁决统计）→ 终审报告原文（report 字段，
    失败或中止可能缺失）→ 发现清单（每条含严重度/类别/位置/描述/依据/裁决/
    裁决理由/修复建议）。全字段容错：键缺失按缺省渲染，正文里不改写内容
    （本来就是 Markdown 文本，原样嵌入）。
    """
    meta = meta if isinstance(meta, dict) else {}
    stats = meta.get("stats") or {}
    roles = [r for r in (meta.get("roles") or []) if isinstance(r, dict)]
    judge = meta.get("judge") or {}
    findings = [f for f in (meta.get("findings") or []) if isinstance(f, dict)]
    status = str(meta.get("status") or "unknown")
    status_label = {"done": "完成", "error": "失败", "cancelled": "已中止"}.get(status, status)
    title = (session_title or "").strip() or "未命名会话"

    lines = [f"# 对抗审查报告：{title}", ""]
    lines.append(f"- 来源：消息 seq {seq} · 状态 {status_label}")
    if judge.get("provider") or judge.get("model"):
        lines.append(f"- 裁判：{judge.get('provider') or '?'} / {judge.get('model') or '?'}")
    if roles:
        lines.append("- 角色名册：" + "、".join(
            f"{_ADV_ROLE_LABELS.get(str(r.get('role')), str(r.get('role') or '?'))}"
            f"（{r.get('provider') or '?'} / {r.get('model') or '?'}）" for r in roles))
    if stats.get("total"):
        bits = [f"共 {stats['total']} 条"]
        for key, label in (("confirmed", "成立"), ("partial", "部分成立"),
                           ("refuted", "推翻"), ("pending", "待定")):
            if stats.get(key):
                bits.append(f"{label} {stats[key]}")
        lines.append("- 裁决统计：" + "，".join(bits))
    report = str(meta.get("report") or "").strip()
    if report:
        lines += ["", "## 终审报告", "", report]
    if findings:
        lines += ["", "## 发现清单", ""]
        for i, f in enumerate(findings, 1):
            head = str(f.get("description") or "").strip().splitlines()
            title = (head[0] if head else "") or str(f.get("id") or "") or "（无描述）"
            lines.append(f"### {i}. {title}")
            lines.append("")
            bits = []
            if f.get("severity"):
                bits.append(f"严重度：{_ADV_SEV_LABELS.get(str(f['severity']), str(f['severity']))}")
            if f.get("category"):
                bits.append(f"类别：{f['category']}")
            if f.get("location"):
                bits.append(f"位置：{f['location']}")
            if bits:
                lines.append("- " + "｜".join(bits))
            if f.get("verdict"):
                verdict = _ADV_VERDICT_LABELS.get(str(f["verdict"]), str(f["verdict"]))
                reason = str(f.get("reason") or "").strip()
                lines.append(f"- 裁决：{verdict}" + (f"（{reason}）" if reason else ""))
            desc = str(f.get("description") or "").strip()
            if desc:
                lines += ["", desc, ""]
            for label, key in (("依据", "evidence"), ("修复建议", "solution")):
                body = str(f.get(key) or "").strip()
                if body:
                    lines += [f"- {label}：", "", "  > " + body.replace("\n", "\n  > "), ""]
    if not report and not findings:
        lines += ["", "（本次对抗没有产出终审报告或发现清单。）"]
    filename = f"对抗审查报告-{seq:04d}.md"
    return "\n".join(lines).rstrip() + "\n", filename


# 团队轮 assistant 气泡里成员报告摘录的长度（全文在频道卡片与 meta 的 messages 里）
TEAM_DIGEST_EXCERPT_CHARS = 300


def _team_digest(orch: TeamOrchestrator, result: dict) -> str:
    """团队轮的 assistant 气泡文本：本轮被唤醒成员的纪要（+ 终态说明）。

    成员发言的正文由 TeamMessage 事件直播、随 meta 的 messages 回放；气泡只
    承载「这轮谁应答了、结果如何」的纪要，终态时附《交付说明》全文。AI 总管
    模式下总管发言同样直播进频道，气泡按推进情况给一句话纪要。
    """
    lines: list[str] = []
    for w in result.get("woke") or []:
        name, status = str(w.get("member", "")), str(w.get("status", ""))
        seq = int(w.get("seq") or 0)
        if status == "done" and seq:
            report = next((m.text for m in orch.channel.messages if m.seq == seq), "")
            excerpt = report[:TEAM_DIGEST_EXCERPT_CHARS]
            if len(report) > TEAM_DIGEST_EXCERPT_CHARS:
                excerpt += f"…（全文见频道 #{seq}）"
            lines.append(f"【{name}】{excerpt}")
        elif status == "error":
            lines.append(f"【{name}】本轮失败（{w.get('error') or '未知原因'}）")
        elif status == "cancelled":
            lines.append(f"【{name}】已停止（已产出的部分已定稿进频道）")
        elif status == "skipped":
            # 轮次耗尽/预算越线在前一名成员轮上触发收队后，剩余点名不再唤醒
            lines.append(f"【{name}】未唤醒（{w.get('error') or '团队已进入终态'}）")
    if not lines:
        if orch.director_mode == "ai":
            # inject_user_message 的返回没有 woke 键（区别于自动循环的空唤醒）
            injected = "woke" not in result
            lines.append(
                "（插话已进入团队频道：总管推进中，将在下一轮优先处理。）" if injected
                else "（总管已处理本轮消息：过程与分工见团队频道。）"
            )
        else:
            lines.append("（消息已进入团队频道；未被点名的队员会在下次被唤醒时看到。）")
    finished = result.get("finished")
    if finished:
        lines.append(str(finished.get("summary") or ""))
    return "\n\n".join(line for line in lines if line)


_ROOM_TASK_STATUSES = ("pending", "in_progress", "review", "done", "error")


def _room_empty_stats() -> dict:
    """team.room 空态的 stats：五个状态全零 + 空成员用量（形状与活动态一致）。"""
    return {"tasks_by_status": {k: 0 for k in _ROOM_TASK_STATUSES}, "member_usage": {}}


def _room_stats(team: dict) -> dict:
    """从团队快照派生团队室首画 stats：工单按状态计数 + 成员用量。

    快照里 status 在五态之外的工单不计入（前端看板把它们折进完成/失败区，
    这里的计数只服务成员带与列头首画，缺一个数比数错一个好）。
    """
    counts = {k: 0 for k in _ROOM_TASK_STATUSES}
    for t in team.get("tasks") or []:
        st = str((t or {}).get("status") or "")
        if st in counts:
            counts[st] += 1
    usage = (team.get("usage") or {}).get("members")
    return {
        "tasks_by_status": counts,
        "member_usage": dict(usage) if isinstance(usage, dict) else {},
    }


class CollabBodyMixin:
    """D2 多模型协作：成员装配 / 圆桌与对抗 / 团队。

    方法自 backend.py 按职责注释整段逐字搬入；状态全部来自主类共享 self
    （本 Mixin 无 __init__、无类级可变属性）。
    """

    # ---- 跨域收敛（批 6，设计 §3.3 雷区 _teams）：三个只读 accessor 与两个写封装 ----
    # 其它域触碰 _teams / _teams_interrupted 一律经下面五个入口（本文件内保持
    # 裸访问）：读点按形态对号——成员判定走 team_active、键式取走
    # team_orchestrator、遍历（values 或 items 键遍历）走 team_orchestrators；
    # 写点只有「删除会话摘队」与「切项目中断」两个语义块。

    def team_active(self, sid: str) -> bool:
        """成员判定：该会话是否有活动团队（`sid in self._teams` 一行透传）。"""
        return sid in self._teams

    def team_orchestrator(self, sid: str) -> TeamOrchestrator | None:
        """键式取活动编排器（`.get(sid)`，无队回 None）。"""
        return self._teams.get(sid)

    def team_orchestrators(self) -> dict[str, TeamOrchestrator]:
        """全部活动编排器：返回 dict 本体（不是 values 视图）。

        遍历读点用 `.values()`；需要键的（迁移判忙）用 `.items()`。getattr
        防御沿既有写法（setup 早期尚未铺字段时按空表处理）。
        """
        return getattr(self, "_teams", {})

    def team_detach_for_delete(self, session_id: str) -> TeamOrchestrator | None:
        """删除会话时摘出团队登记（与 delete_session 原块逐字等价）。

        pop 编排器 → pop 中断记录 → 有摘除或有落盘路径时回写团队状态。返回
        被摘的编排器：调用方（lifecycle.delete_session）要等在飞轮收尾之后才
        释放它的 provider，避免收尾代码撞上「连接已关闭」。
        """
        orch = self._teams.pop(session_id, None)
        self._teams_interrupted.pop(session_id, None)
        if orch is not None or self._team_state_path is not None:
            self._persist_team_state()
        return orch

    def team_interrupt_for_project_switch(self) -> None:
        """切项目时把旧项目的主栏团队登记转「已中断」（与 _bind_project 原块逐字等价）。

        跨项目分屏列的团队豁免：成员挂的是会话所属项目（_own_ctx）的工具表与
        权限门，不随主栏切项目失效。其余会话 pop 编排器 → 释放成员 provider →
        登记中断注记（含 team_id，供 team.get 展示与频道回放）→ 条件回写。
        """
        if self._teams:
            for sid in [
                sid for sid in list(self._teams)
                if not ((rt := self.runtimes.get(sid)) is not None
                        and rt.ctx is not None and rt.ctx.own)
            ]:
                orch = self._teams.pop(sid)
                self._release_team_providers(orch)
                self._teams_interrupted[sid] = {
                    "session_id": sid,
                    "team_id": orch.team_id,
                    "roster": [m.name for m in orch.roster],
                    "director_mode": orch.director_mode,
                    "status": "interrupted",
                    "interrupt_reason": "项目已切换，团队已中断",
                }
            if self._teams or self._teams_interrupted:
                self._persist_team_state()

    # ---- 圆桌：多模型并行独立作答 + 主席融合 ----

    def _build_member_provider(self, name: str, model: str | None) -> Provider:
        """为圆桌成员构建独立 Provider 实例（无副作用：不改当前使用的模型状态）。"""
        if self._provider_factory_override is not None:  # 测试/演示注入
            return self._provider_factory_override()
        if name not in self.cfg.providers:
            raise ConfigError(
                "unknown provider: {}; available: {}".format(name, ", ".join(self.cfg.providers))
            )
        pc = self.cfg.providers[name]
        if model:
            pc = pc.model_copy(update={"model": model})
        return build_provider(name, pc, self.cfg.network)

    # 自动池「坏服务降权」：鉴权/余额类失败不是偶发，重试同一个服务只会
    # 再失败一次。配置里留着一个 401 Key 时，对抗的发现者游标一旦挑到它，
    # 整场会按 fail-fast 直接报错（真实模型走查）。这里只排序不剔除——
    # 全都坏时仍要给出错误角色卡，而不是假装没有可用服务。
    _BAD_POOL_CATEGORIES = frozenset({"auth", "balance"})

    def _auto_pool_rank(self, name: str) -> int:
        """自动池排序权重：0 可用/未测、1 有隐患（限流等）、2 鉴权/余额坏。"""
        try:
            health = self.provider_health.get(name)
        except Exception:  # noqa: BLE001 - 健康灯不可用不拦自动挑模型
            return 0
        if health is None:
            return 0
        if health.level == "down" and health.category in self._BAD_POOL_CATEGORIES:
            return 2
        if health.level != "ok":
            return 1
        return 0

    def _auto_pool_services(self) -> list[tuple[str, str]]:
        """已配置 Key 的服务池（按健康度稳定排序：能用的排前面）。"""
        pool: list[tuple[str, str]] = []
        for name, pc in self.cfg.providers.items():
            try:
                if resolve_api_key(name, pc) is None:
                    continue
            except Exception:  # noqa: BLE001 - 探测失败按未配置处理
                continue
            pool.append((name, pc.model or ""))
        # sorted 是稳定排序：同档服务保持配置顺序（自动挑模型的确定性不变）
        return sorted(pool, key=lambda item: self._auto_pool_rank(item[0]))

    def _resolve_members(
        self, members_params: list | None, chair_answers: bool | None = None,
        chair_key: tuple[str, str] | None = None, limit: int | None = None,
    ) -> list[MemberSpec]:
        """解析圆桌成员：显式列表优先；缺省时取所有已配置 Key 的服务的当前模型。

        规则：
        - 去重（同名同模型只留一个）；上限 cfg.roundtable.max_members（不含主席）；
        - chair_answers 为 None 时用配置值；为 True 时跳过与主席重复的成员
          （主席会被单独插到队列最前）；
        - chair_key 是主席的 (服务, 模型) 去重键：缺省按全局记账（团队等
          非会话轮），圆桌轮按本轮会话解析传入（见 _roundtable_body）；
        - 单个成员构建失败（缺 Key/未知服务）不阻断，作答时以错误卡片呈现。
        """
        specs: list[MemberSpec] = []
        seen: set[tuple[str, str]] = set()
        # 模板可收紧自动成员池的上限：0 / None 都按 [roundtable] 配置走
        limit = self.cfg.roundtable.max_members if not limit else max(0, int(limit))
        chair_in = (
            self.cfg.roundtable.chair_answers if chair_answers is None
            else bool(chair_answers)
        )
        if chair_key is None:
            chair_key = (self.provider_name, self.provider_model)

        def _add(name: str, model: str, role: str = "") -> None:
            key = (name, model)
            if key in seen or len(seen) >= limit:
                return
            seen.add(key)
            if role not in MEMBER_ROLES:
                role = ""  # 未知身份 id 一律按普通成员处理
            try:
                provider = self._build_member_provider(name, model)
                specs.append(MemberSpec(
                    provider_name=name,
                    model=model or getattr(provider, "model", ""),
                    provider=provider,
                    role=role,
                ))
            except Exception as e:  # noqa: BLE001 - 构建失败降级为错误成员卡片
                specs.append(MemberSpec(
                    provider_name=name, model=model, build_error=str(e)[:200], role=role,
                ))

        if members_params:
            for m in members_params:
                name = str((m or {}).get("provider", "")).strip()
                if not name:
                    continue
                model = str((m or {}).get("model", "") or "").strip()
                role = str((m or {}).get("role", "") or "").strip()
                if chair_in and (name, model) == chair_key:
                    continue
                _add(name, model, role)
        else:
            for name, model in self._auto_pool_services():
                if chair_in and (name, model) == chair_key:
                    continue
                _add(name, model)
        return specs

    # 「提到就带上」预取与圆桌轮主体已拆到 backend_parts/roundtable_body.py（O5）
    # ---- 圆桌设置（设置 · 圆桌）：读回当前值 → 编辑 → 保存后热生效 ----

    def roundtable_detail(self) -> dict:
        """设置页圆桌卡片的数据。"""
        rt = self.cfg.roundtable
        return {
            "max_members": rt.max_members,
            "member_timeout_s": rt.member_timeout_s,
            "chair_answers": rt.chair_answers,
            "debate_rounds": rt.debate_rounds,
            "member_history_turns": rt.member_history_turns,
            "configured_services": len(self._configured_services()),
            "config_hint": (
                "圆桌让多个模型并行独立作答，再由主席（当前主模型）汇总成一份答案，"
                "token 成本约为单模型的「成员数 + 1」倍；每多一轮辩论修订，成员成本"
                "再翻一倍（观点已一致的成员会自动跳过，雷同草稿在融合时自动去重）。"
                "成员来自「模型服务」里已配好 Key 的服务，可在输入框的圆桌面板逐个"
                "勾选并指定身份（批评者 / 事实核查员等）。圆桌全程不调用工具、不写文件。"
            ),
        }

    async def roundtable_save(self, params: dict) -> dict:
        """保存圆桌设置并热生效（写 config.toml 的 [roundtable] 段）。"""
        updates: dict = {}
        if params.get("max_members") is not None:
            updates["max_members"] = max(1, min(8, int(params["max_members"])))
        if params.get("member_timeout_s") is not None:
            updates["member_timeout_s"] = max(10, int(params["member_timeout_s"]))
        if params.get("chair_answers") is not None:
            updates["chair_answers"] = bool(params["chair_answers"])
        if params.get("debate_rounds") is not None:
            updates["debate_rounds"] = max(0, min(2, int(params["debate_rounds"])))
        if params.get("member_history_turns") is not None:
            updates["member_history_turns"] = max(0, int(params["member_history_turns"]))
        if updates:
            update_config_section("roundtable", updates)
            self.cfg = load_config()
        return self.roundtable_detail()

    # ---- 对抗：四角色流水线审查（docs/对抗模式设计.md） ----
    # 与圆桌（会诊融合）、团队（分工协作）并列的第三种多模型协作。角色解析：
    # members 条目可带 role="finder"/"investigator"/"advisor" 逐角色显式指定
    # 模型（弹层三个下拉），同一模型可身兼数角（「同一 AI 自我对抗」的路径）；
    # 未指定的角色回退按序自动映射（缺省取所有已配置 Key 的服务）。裁判缺省用
    # 当前主模型（同圆桌主席）。编排本体在 core/adversarial.py，这里只做装配与落库。

    async def _adversarial_body(
        self, text: str, emit: EmitFn, members_params: list | None,
        agent: Agent | None = None, sid: str | None = None,
        images: list[ImageBlock] | None = None, regenerate: bool = False,
        template: dict | None = None,
    ) -> dict:
        """对抗轮主体：发现 → 调查 → 建议 → 裁判 → 报告并入主历史。

        返回随轮次结果回传前端的对抗元数据（角色名册、裁决统计、问题清单、
        终审报告原文）。
        历史追加 user(被审内容) + assistant(最终报告)；中间产物（问题、裁决、
        方案）随报告消息的 adversarial 元数据持久化，历史回放时对抗卡可展开
        回看。取消语义与圆桌对齐：user 消息先入历史，中途点停止内容不丢；
        裁判中途取消时已流出的部分报告文本照样落库。
        """
        if agent is None:
            agent = self.agent
        if self.provider is None:
            raise RuntimeError("对抗需要当前主模型可用；请先在模型下拉中选择一个已配置 Key 的服务")

        tpl = template if isinstance(template, dict) else None
        tpl_limits = (tpl or {}).get("limits") or {}
        tpl_agg = (tpl or {}).get("aggregator") or {}
        cfg = self.cfg.adversarial
        # 模板限额：0 = 不覆盖 [adversarial] 配置
        max_findings = int(tpl_limits.get("max_findings") or 0) or cfg.max_findings
        role_timeout_s = int(tpl_limits.get("role_timeout_s") or 0) or cfg.role_timeout_s
        # 裁判按本轮会话解析（同 _roundtable_body 的主席口径）：会话级换过模型
        # 的标签/分屏列，终审必须由会话自己的模型做，而不是全局记账的模型
        resident_judge = agent.provider if agent.provider is not None else self.provider
        judge = resident_judge
        if sid:
            judge_name, judge_model = self._session_effective_model(sid)
        else:
            judge_name, judge_model = self.provider_name or "", self.provider_model or ""
        # 模板的收口角色（aggregator）非空时替换裁判（临时 provider，轮末释放）
        template_judge = None
        if tpl is not None and str(tpl_agg.get("provider") or "").strip():
            judge_name = str(tpl_agg.get("provider") or "").strip()
            judge_model = str(tpl_agg.get("model") or "").strip()
            try:
                template_judge = self._build_member_provider(judge_name, judge_model)
            except Exception as e:  # noqa: BLE001 - 裁判不可用要可读报错
                raise RuntimeError(
                    f"模板的收口模型「{judge_name}/{judge_model or '默认模型'}」不可用：{e}"
                ) from e
            judge = template_judge
            if not judge_model:
                judge_model = getattr(template_judge, "model", "") or ""
        budget = RunBudget(**((tpl or {}).get("budget") or {})) if tpl is not None else None

        emit_ev = _dump_emit(emit)

        adv_t0 = time.monotonic()

        def _adv_ms() -> int:
            return int((time.monotonic() - adv_t0) * 1000)

        await emit_ev(TurnStarted(iteration=1))

        # 对抗是纯文本协作：图片不发给角色模型，但要让用户知道，不静默丢弃
        if images:
            await emit_ev(NoticeEvent(
                message=f"对抗轮为纯文本协作：本轮的 {len(images)} 张图片不会发给角色模型"
                        "（已随消息保留）；需要模型看图请关闭对抗后重发。"
            ))

        # 用户消息先入历史（与普通轮一致）：中途停止问题也能落库，不丢上下文。
        # 重新生成时提问已在历史末尾（session.truncate 已删掉旧回答），不再重复追加。
        if regenerate:
            question = text
            for m in reversed(agent.history):
                if m.role == "user" and m.text.strip():
                    question = m.text
                    break
        else:
            question = text
            agent.history.append(Message.user(text, images))

        # 纯文本对抗：角色没有文件工具，「审查 calc.py」这类提问若上下文没读过
        # 该文件，角色只能拒绝——发起前把点名文件只读预取进被审目标（不改落库
        # 的用户消息，也不写盘）。
        adv_text = question
        preload = self._preload_mentioned_files(
            question, agent.history, getattr(agent, "working_dir", None)
        )
        if preload:
            adv_text = f"{question}\n\n{preload}"
            await emit_ev(NoticeEvent(message=(
                "对抗角色没有文件工具：已把你点名的项目文件（只读）带给角色，"
                "最多 3 个，过长会截取。"
            )))

        # 上下文压缩护栏：四个角色都吃会话上下文，超限先压缩（同圆桌）
        if agent.used_context_tokens() > agent.context_limit_tokens:
            ev = await compact_history(
                agent, keep_recent=self.cfg.compaction_keep_recent, budget=budget,
            )
            if ev is not None:
                await emit_ev(ev)

        # 角色解析：members 条目可带 role="finder"/"investigator"/"advisor"
        # 显式指定该角色的模型（弹层逐角色下拉）；带角色的条目直接构建、不参与
        # 去重（同一模型可身兼数角正是常态），其余角色回退到自动策略——无角色
        # 条目按序映射（chair 不回避：裁判与角色同服务是正常形态，不剔除）。
        explicit: dict[str, tuple[str, str]] = {}
        auto_params: list = []
        for m in members_params or []:
            entry = m if isinstance(m, dict) else {}
            role_key = str(entry.get("role", "") or "").strip()
            name = str(entry.get("provider", "") or "").strip()
            if role_key in ("finder", "investigator", "advisor") and name \
                    and role_key not in explicit:
                explicit[role_key] = (name, str(entry.get("model", "") or "").strip())
            elif name:
                auto_params.append(entry)

        def _explicit_spec(name: str, model: str) -> MemberSpec:
            try:
                provider = self._build_member_provider(name, model)
                return MemberSpec(
                    provider_name=name,
                    model=model or getattr(provider, "model", ""),
                    provider=provider,
                )
            except Exception as e:  # noqa: BLE001 - 构建失败降级为错误角色卡
                return MemberSpec(provider_name=name, model=model, build_error=str(e)[:200])

        # 模板的角色落位（带边界）：seat 0/1/2 = 发现/调查/建议。带 phase key
        # 的角色优先归位；没有 phase key 的按出现顺序补空位；超过三个的角色
        # 本期不参与（对抗只有三个角色），如实计入 dropped（preview 同口径）。
        tpl_seat_roles: dict[str, dict] = {}
        tpl_extra_roles: list[dict] = []
        for role in (tpl or {}).get("roles") or []:
            if not isinstance(role, dict):
                continue
            key = str(role.get("key") or "").strip()
            if key in (PHASE_FINDER, PHASE_INVESTIGATOR, PHASE_ADVISOR) \
                    and key not in tpl_seat_roles:
                tpl_seat_roles[key] = role
            else:
                tpl_extra_roles.append(role)
        for phase in (PHASE_FINDER, PHASE_INVESTIGATOR, PHASE_ADVISOR):
            if phase not in tpl_seat_roles and tpl_extra_roles:
                tpl_seat_roles[phase] = tpl_extra_roles.pop(0)

        # 自动序列：未带 members（纯自动）或带普通条目时才解析；模板已把三个
        # 角色全部指定为具体服务时不白建 Provider（也避免占掉测试注入的
        # provider 工厂池）。
        need_auto_pool = tpl is None or any(
            phase not in tpl_seat_roles
            or not str((tpl_seat_roles[phase] or {}).get("provider") or "").strip()
            for phase in (PHASE_FINDER, PHASE_INVESTIGATOR, PHASE_ADVISOR)
        )
        specs_auto = (
            self._resolve_members(
                auto_params, False, chair_key=(judge_name, judge_model),
            )
            if (members_params is None or auto_params) and need_auto_pool else []
        )

        # 未显式指定的角色按 固定角色顺序 从自动序列依次取（游标递进）：全不指定
        # 时等价于旧的 0/1/2 取模复用（自动服务不足时同一模型身兼数角）；
        # 部分指定时剩余角色也按序补位，不会跳号。
        auto_cursor = 0

        def _role_spec(role: str) -> MemberSpec:
            nonlocal auto_cursor
            if role in explicit:
                return _explicit_spec(*explicit[role])
            if not specs_auto:
                return MemberSpec(
                    provider_name="", model="",
                    build_error="未指定模型且没有已配置 Key 的服务",
                )
            spec = specs_auto[auto_cursor % len(specs_auto)]
            auto_cursor += 1
            return spec

        def _template_role_spec(role: dict) -> MemberSpec:
            """模板角色 → MemberSpec：指定服务则新建，留空按自动池游标补位。"""
            nonlocal auto_cursor
            name = str(role.get("name") or "").strip()
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            if provider_name:
                spec = _explicit_spec(provider_name, model)
                spec.name = name
                return spec
            if not specs_auto:
                return MemberSpec(
                    name=name, provider_name="", model="",
                    build_error="未指定模型且没有已配置 Key 的服务",
                )
            base = specs_auto[auto_cursor % len(specs_auto)]
            auto_cursor += 1
            return MemberSpec(
                name=name, provider_name=base.provider_name, model=base.model,
                provider=base.provider, build_error=base.build_error,
            )

        finder = (
            _template_role_spec(tpl_seat_roles[PHASE_FINDER])
            if PHASE_FINDER in tpl_seat_roles else _role_spec(PHASE_FINDER)
        )
        investigator = (
            _template_role_spec(tpl_seat_roles[PHASE_INVESTIGATOR])
            if PHASE_INVESTIGATOR in tpl_seat_roles else _role_spec(PHASE_INVESTIGATOR)
        )
        advisor = (
            _template_role_spec(tpl_seat_roles[PHASE_ADVISOR])
            if PHASE_ADVISOR in tpl_seat_roles else _role_spec(PHASE_ADVISOR)
        )
        # 角色人设：只放非空值（空串必须回落 adversarial.py 的内置默认提示词，
        # 不能让「模板留空」把不对称激励整段抹掉）；收口 prompt 归裁判。
        prompts: dict[str, str] = {}
        for phase, role in tpl_seat_roles.items():
            text = str((role or {}).get("prompt") or "").strip()
            if text:
                prompts[phase] = text
        if tpl is not None:
            agg_prompt = str(tpl_agg.get("prompt") or "").strip()
            if agg_prompt:
                prompts[PHASE_JUDGE] = agg_prompt
        if all(s.provider is None for s in (finder, investigator, advisor)):
            await emit_ev(TurnFinished(stop_reason="error", iterations=1, duration_ms=_adv_ms()))
            raise RuntimeError(
                "对抗没有可用角色：请先在「设置 · 模型服务」配置 API Key，"
                "或在对抗面板为角色指定模型"
            )

        # 发现者替补（只给自动挑选路径）：发现阶段失败=整场 fail-fast，而
        # 自动池可能正好挑到坏 Key 的服务。显式指定/模板指定服务的角色不给
        # 替补——用户的选择不该被静默替换。具体换谁由 core 过滤同服务后决定。
        finder_fallbacks: list[MemberSpec] = []
        finder_is_auto = PHASE_FINDER not in explicit and (
            PHASE_FINDER not in tpl_seat_roles
            or not str((tpl_seat_roles.get(PHASE_FINDER) or {}).get("provider") or "").strip()
        )
        if finder_is_auto:
            finder_fallbacks = [s for s in specs_auto if s.provider is not None]

        # 角色都不看图片（纯文本协作），历史里的图片块单独剥离；
        # history[:-1] 排除刚追加的本次提问（由 run_adversarial 自己拼在末尾）。
        # 角色消息不接会话系统提示词：角色系统提示词自足（core/adversarial.py
        # 的 _phase_messages），dialogue() 会把 history 里的 system 剥掉。
        role_history = _strip_image_blocks(agent.history[:-1])

        try:
            outcome: AdversarialOutcome = await run_adversarial(
                finder=finder,
                investigator=investigator,
                advisor=advisor,
                judge=judge,
                judge_provider=judge_name or "",
                judge_model=judge_model or "",
                history=role_history,
                user_text=adv_text,
                timeout_s=role_timeout_s,
                emit=emit_ev,
                max_findings=max_findings,
                prompts=prompts,
                budget=budget,
                finder_fallbacks=finder_fallbacks,
            )
        finally:
            # 角色临时 provider 随轮释放（同圆桌：裁判是用主 Agent 的常驻实例；
            # 模板收口裁判是本轮临时构建的，必须进释放清单）
            release = [finder.provider, investigator.provider, advisor.provider]
            if template_judge is not None:
                release.append(template_judge)
            self._release_turn_providers(release, keep=(resident_judge,))

        meta = {
            "mode": "adversarial",
            "judge": {"provider": judge_name, "model": judge_model},
            "roles": [
                {"role": "finder", "provider": finder.provider_name,
                 "model": finder.model, "name": getattr(finder, "name", "")},
                {"role": "investigator", "provider": investigator.provider_name,
                 "model": investigator.model,
                 "name": getattr(investigator, "name", "")},
                {"role": "advisor", "provider": advisor.provider_name,
                 "model": advisor.model, "name": getattr(advisor, "name", "")},
                {"role": "judge", "provider": judge_name, "model": judge_model},
            ],
            "stats": adversarial_stats(outcome),
            "findings": adversarial_findings_meta(outcome),
            # 终审报告原文随 meta 落库（与消息正文同源）：adversarial.export
            # 的「## 终审报告」节只认库内 meta（_adversarial_report_markdown），
            # 不落这里导出文件就永远只有发现清单。裁判失败的兜底报告在下方
            # 生成后覆写同键（degraded=True 已注明非终审）。
            "report": outcome.report,
            "status": outcome.status,
            "error": outcome.error,
        }
        if tpl is not None:
            # 模板身份随消息 meta 落库：历史回放卡头仍能显示「模板：xxx」
            meta["template"] = str(tpl.get("name") or "")
            meta["template_kind"] = "adversarial"
        if budget is not None and budget.reason():
            # 预算耗尽：报告（若有）照常保留在 outcome.report / meta["report"]，
            # 状态如实标 error 并带上原因码
            meta["budget_exceeded"] = budget.reason()

        # 用量入账：对抗不走 agent.run_turn，agent.total_* 不会动。按角色逐条
        # 写 usage_log（裁判记当前主模型名下），统计页与预算护栏才看得见真实成本。
        if sid:
            try:
                for row in adversarial_usage_rows(outcome):
                    await self.store.add_usage(
                        sid, row["provider"], row["model"],
                        row["input_tokens"], row["output_tokens"],
                        row.get("cached_tokens", 0),
                    )
            except Exception:  # noqa: BLE001 - 记账失败不影响本轮结果
                pass
            await self._check_budget_alerts()

        report = outcome.report
        if not report.strip() and outcome.findings:
            # 裁判失败/取消且没流出任何报告文本，但问题清单在：用中间结果拼一份
            # 如实的兜底报告（不再调模型）——已花的钱与已得的裁决不随刷新消失。
            s = adversarial_stats(outcome)
            lines = ["## 对抗审查报告（终审未完成，以下为复核后的中间结果）\n"]
            for f in outcome.findings:
                verdict = {
                    "confirmed": "成立", "refuted": "已推翻",
                    "partial": "部分成立", "pending": "待定",
                }.get(f.verdict, f.verdict)
                lines.append(
                    f"- **{f.finding_id}**（{verdict}）[{f.effective_severity}] "
                    f"{f.category} @ {f.location or '未标注'}：{f.effective_description}"
                )
                if f.solution:
                    lines.append(f"  - 修复方案：{f.solution}")
            lines.append(
                f"\n共 {s['total']} 条候选：成立 {s['confirmed']}、部分成立 "
                f"{s['partial']}、推翻 {s['refuted']}、待定 {s['pending']}。"
            )
            report = "\n".join(lines)
            # 兜底报告同样落 meta 的 report 键：导出的「终审报告」节在裁判
            # 失败/取消路径也有事实源（degraded=True 已如实标注非终审）
            meta["report"] = report
            meta["degraded"] = True

        if report.strip():
            assistant = Message.assistant([TextBlock(text=report)])
            assistant.adversarial = meta
            agent.history.append(assistant)
            await emit_ev(AssistantMessage(message=assistant.model_dump()))

        if outcome.status == "cancelled":
            # 用户中途停止：已流出的部分报告已落库；TurnFinished 由 pipeline
            # 的 stopped 结果收尾。
            await emit_ev(TurnFinished(
                stop_reason="cancelled", iterations=1, duration_ms=_adv_ms(),
            ))
            return meta

        if outcome.status == "error" and not report.strip():
            # 发现者失败且没有任何可保留的产出：如实报错收尾。
            # 落一条标注失败的留痕消息再收尾——只发 TurnFinished(error) 的话
            # 历史里没有 assistant 行，刷新后本轮对抗经历完全消失（用户感知
            # 是「发出去的话石沉大海」）。留痕正文不含编造的结论；meta 照常
            # 带 status=error，历史回放按 err 样式重建对抗卡。
            note = "本轮对抗审查未完成"
            err_text = str(outcome.error or "").strip()
            if err_text:
                note += f"：{err_text}"
            assistant = Message.assistant([TextBlock(text=note)])
            assistant.adversarial = meta
            agent.history.append(assistant)
            await emit_ev(AssistantMessage(message=assistant.model_dump()))
            await emit_ev(TurnFinished(
                stop_reason="error", iterations=1, duration_ms=_adv_ms(),
            ))
            return meta

        await emit_ev(TurnFinished(
            stop_reason="end_turn", iterations=1, duration_ms=_adv_ms(),
        ))
        return meta

    # ---- 对抗设置（设置 · 对抗）：读回当前值 → 编辑 → 保存后热生效 ----

    def adversarial_detail(self) -> dict:
        """设置页对抗卡片的数据。"""
        adv = self.cfg.adversarial
        return {
            "max_findings": adv.max_findings,
            "role_timeout_s": adv.role_timeout_s,
            "configured_services": len(self._configured_services()),
            "config_hint": (
                "对抗让多个模型接力审查：发现者只管找问题（宁滥勿缺），"
                "调查者逐条验证（力求推翻误报），建议者给修复方案，裁判"
                "（当前主模型）过滤噪音并产出终审报告。成员按勾选顺序承担前三个"
                "角色，不足时自动复用同一个模型——单一模型也能自我对抗。全程纯"
                "文本协作，不调用工具、不写文件。成本约为「角色数 + 1」次调用。"
            ),
        }

    async def adversarial_save(self, params: dict) -> dict:
        """保存对抗设置并热生效（写 config.toml 的 [adversarial] 段）。"""
        updates: dict = {}
        if params.get("max_findings") is not None:
            updates["max_findings"] = max(1, min(100, int(params["max_findings"])))
        if params.get("role_timeout_s") is not None:
            updates["role_timeout_s"] = max(10, int(params["role_timeout_s"]))
        if updates:
            update_config_section("adversarial", updates)
            self.cfg = load_config()
        return self.adversarial_detail()

    async def adversarial_export(self, params: dict, local: bool = True) -> dict:
        """adversarial.export：对抗审查报告导出（P3），返回 {filename, markdown}。

        来源校验与 messages.earlier 同口径：seq + 会话归属（本机任意会话可读，
        远程只认自己名下/当前项目的会话）；报告内容一律从库内 meta 组装，
        不接受客户端提交的正文。
        """
        sid = str(params.get("session_id", "") or "")
        if local:
            sess = await self.store.get_session(sid)
            if sess is None:
                raise RuntimeError("session not found: " + sid)
        else:
            sess = await self._get_owned_session(sid)
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise RuntimeError("无效的对抗消息序号") from None
        if seq <= 0:
            raise RuntimeError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise RuntimeError("指定消息不是对抗审查结果")
        markdown, filename = _adversarial_report_markdown(sess.title, message.adversarial, seq)
        return {"filename": filename, "markdown": markdown}

    async def adversarial_fix_prompt(self, params: dict, local: bool = True) -> dict:
        """adversarial.fix_prompt：把「成立 / 部分成立」的发现拼成一段修复指令。

        返回 {prompt, count}，前端回填输入框（不自动发送——修不修、在哪个会话
        修、用普通回合还是团队，由用户决定，不制造意外开销与权限动作）。
        事实源与导出同口径：只认库内 meta，不接受客户端提交的正文；seq 与
        会话归属校验也一致。
        """
        sid = str(params.get("session_id", "") or "")
        if local:
            sess = await self.store.get_session(sid)
            if sess is None:
                raise RuntimeError("session not found: " + sid)
        else:
            sess = await self._get_owned_session(sid)
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise RuntimeError("无效的对抗消息序号") from None
        if seq <= 0:
            raise RuntimeError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise RuntimeError("指定消息不是对抗审查结果")
        wanted = {k for k in str(params.get("verdicts", "") or "").split(",") if k}
        if not wanted:
            wanted = {"confirmed", "partial"}
        findings = [
            f for f in (message.adversarial.get("findings") or [])
            if isinstance(f, dict) and str(f.get("verdict") or "") in wanted
        ]
        if not findings:
            raise RuntimeError("本次审查没有「成立 / 部分成立」的发现可修复")
        lines = [
            "以下是对抗审查认定**成立**的问题（来自本会话第 "
            f"{seq} 条审查结果）。请逐条修复，修完用一句话回报每条的处理结果：",
            "",
        ]
        for i, f in enumerate(findings, start=1):
            verdict = {"confirmed": "成立", "partial": "部分成立"}.get(
                str(f.get("verdict") or ""), str(f.get("verdict") or "")
            )
            lines.append(
                f"{i}. [{f.get('severity') or 'medium'}·{verdict}] {f.get('category') or ''}"
                f" @ {f.get('location') or '未标注'}"
            )
            lines.append(f"   问题：{str(f.get('description') or '').strip()}")
            if f.get("solution"):
                lines.append(f"   建议方案：{str(f.get('solution')).strip()}")
            lines.append("   验收：改完后能说明「改了什么、为什么这样就修好了」")
        lines.append("")
        lines.append("只改这些成立项，不要顺手重构其他部分；改文件与跑命令照常走确认。")
        return {"prompt": "\n".join(lines), "count": len(findings)}

    async def adversarial_compare(self, params: dict, local: bool = True) -> dict:
        """adversarial.compare：本次对抗审查与「同会话上一次」的对比（P3 剩余半边）。

        对比口径只看落库 meta（与导出同一事实源），不猜：
        - 计数：发现总数、各裁决档位（成立/部分成立/推翻/待定）的增减；
        - 清单：按「位置 + 描述首行」认指纹，算出**新增**与**已消失**的发现
          （第二次审查把上次的问题修掉了，就是「已消失」）。
        找上一次的方式：取该消息之前最近窗口里的对抗结果（最多回看 200 条），
        找不到就返回 previous=None（首次审查，前端只说「这是第一次」）。
        来源校验与导出同口径：seq + 会话归属。
        """
        sid = str(params.get("session_id", "") or "")
        if local:
            sess = await self.store.get_session(sid)
            if sess is None:
                raise RuntimeError("session not found: " + sid)
        else:
            sess = await self._get_owned_session(sid)
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise RuntimeError("无效的对抗消息序号") from None
        if seq <= 0:
            raise RuntimeError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise RuntimeError("指定消息不是对抗审查结果")

        previous = None
        window, _first, _last = await self.store.load_messages_window(
            sid, before_seq=seq, limit=200,
        )
        for candidate in reversed(window):
            if candidate.adversarial:
                previous = candidate
                break
        cur = _adversarial_fingerprints(message.adversarial)
        if previous is None:
            return {
                "current": cur["summary"], "current_seq": seq,
                "previous": None, "previous_seq": 0,
                "delta": {}, "added": [], "resolved": [], "common": len(cur["keys"]),
            }
        prev = _adversarial_fingerprints(previous.adversarial)
        added_keys = cur["keys"] - prev["keys"]
        resolved_keys = prev["keys"] - cur["keys"]
        delta = {
            key: cur["summary"][key] - prev["summary"][key]
            for key in ("total", "confirmed", "partial", "refuted", "pending")
        }
        return {
            "current": cur["summary"], "current_seq": seq,
            "previous": prev["summary"], "previous_seq": int(getattr(previous, "seq", 0) or 0),
            "delta": delta,
            "added": [cur["titles"][k] for k in sorted(added_keys)],
            "resolved": [prev["titles"][k] for k in sorted(resolved_keys)],
            "common": len(cur["keys"] & prev["keys"]),
        }

    # ---- 团队：用户总管的多模型分工协作（一期 MVP，docs/团队模式设计.md） ----
    # 与圆桌并列的第二种多模型协作。活动团队是会话级状态（_teams：sid → 编排器，
    # 内存态）：团队进行中该会话的消息全部进团队频道（用户总管逐个唤醒被点名
    # 成员；AI 总管经 run_auto_turn 驱动自动闭环），工单板只由用户经
    # team.get / team.task_add / team.task_update / team.takeover / team.stop
    # / team.deliver 触达。三期另开三面：频道消息逐条落库（message_sink →
    # team_messages 表，team.log 按 team_id 回放）、建队模板（teams.json，
    # team.template_list/save/remove）、设置页配置卡（teamcfg.get/save）。

    def _load_team_state(self) -> None:
        """重启恢复：上次进程退出时还活动的团队一律标「已中断」（沿任务簿先例）。

        团队的运行时（成员 Agent / 频道 / 工单板）只在内存，不跨进程恢复；
        这里恢复「曾有个团队」的事实（含 team_id，供只读频道回放）供 team.get
        展示。已中断记录本身也持久保留（回写进同一文件的 interrupted 段）——
        否则第二次重启后团队卡就失去 team_id，历史里的频道无法再全量回放。
        记录按会话去重，最多保留 _TEAM_INTERRUPTED_KEEP 条。
        """
        if self._team_state_path is None or not self._team_state_path.exists():
            return
        try:
            data = json.loads(read_text_file(self._team_state_path).text)
        except (OSError, ValueError):
            return  # 坏文件按没有活动团队处理（write_text_atomic 保证不会有半个文件）
        for item in (data or {}).get("interrupted") or []:
            if not isinstance(item, dict) or not item.get("session_id"):
                continue
            rec = dict(item)
            rec["status"] = "interrupted"
            rec.setdefault("interrupt_reason", "应用重启，团队已中断")
            self._teams_interrupted[str(item["session_id"])] = rec
        for item in (data or {}).get("teams") or []:
            if not isinstance(item, dict) or not item.get("session_id"):
                continue
            rec = dict(item)
            rec["status"] = "interrupted"
            rec["interrupt_reason"] = "应用重启，团队已中断"
            self._teams_interrupted[str(item["session_id"])] = rec
        if self._teams_interrupted:
            self._persist_team_state()  # 活动段清账，中断段（含 team_id）回写保留

    def _persist_team_state(self) -> None:
        """活动团队与已中断团队登记落盘（textio 原子写；失败不拖垮团队本身）。"""
        if self._team_state_path is None:
            return
        # 已中断记录按插入序保留最近的若干条（同一会话只留最新一份）
        interrupted = list(self._teams_interrupted.values())[
            -_TEAM_INTERRUPTED_KEEP:
        ]
        data = {
            "teams": [
                {"session_id": sid,
                 "team_id": orch.team_id,
                 "roster": [m.name for m in orch.roster],
                 "director_mode": orch.director_mode}
                for sid, orch in self._teams.items()
            ],
            "interrupted": interrupted,
        }
        try:
            write_text_atomic(
                self._team_state_path,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError:
            pass

    def _release_team_providers(self, orch) -> None:
        """释放团队持有的成员 provider（httpx 连接池）。

        队员 provider 在建队时构建一次、跨整个团队生命周期常驻（成员 Agent
        复用同一实例），团队关闭时必须释放，否则每开一队泄漏一批连接池。
        AI 总管的 provider 同理（DirectorSpec.provider）。主持/常驻对象（如
        team.takeover 后 AI 总管转顾问）仍按本方法统一释放——团队已关，无处
        再用。
        """
        if orch is None:
            return
        provs = []
        for spec in getattr(orch, "roster", []) or []:
            provs.append(getattr(spec, "provider", None))
        director = getattr(orch, "_director", None)
        if director is not None:
            provs.append(getattr(director, "provider", None))
        for ag in orch.agents():
            provs.append(getattr(ag, "provider", None))
        self._release_turn_providers(provs)

    def _team_provider_context_limit(self, name: str) -> int:
        """成员/总管模型的上下文窗口（按 provider 配置；0 = 编排器全局回退）。

        ``_resolve_team_members`` 可能在 setup 之前被直接调用（测试/工具），
        此时 cfg 尚未装载：回 0 让编排器用全局值，不要因缺配置炸掉解析。
        """
        cfg = self.cfg
        if cfg is None:
            return 0
        pc = cfg.providers.get(name)
        return pc.effective_context_limit(cfg.context_limit_tokens) if pc \
            else cfg.context_limit_tokens

    def _resolve_team_members(self, members_params: list | None) -> list[TeamMemberSpec]:
        """解析团队队员：显式列表优先（可带 name / persona）；缺省沿圆桌的自动解析。

        显式成员与圆桌同名同模型去重；单个成员构建失败（缺 Key/未知服务）不阻断，
        入册为错误成员、唤醒时按失败隔离呈现（与圆桌错误卡片同姿态）。缺省路径
        chair_answers=False：团队没有主席模型，当前主模型不参与过滤。
        """
        specs: list[TeamMemberSpec] = []
        if members_params:
            seen: set[tuple[str, str]] = set()
            for m in members_params:
                name = str((m or {}).get("provider", "")).strip()
                if not name:
                    continue
                model = str((m or {}).get("model", "") or "").strip()
                key = (name, model)
                if key in seen:
                    continue
                seen.add(key)
                member_name = str((m or {}).get("name", "") or "").strip()
                persona = str((m or {}).get("persona", "") or "").strip()
                if len(persona) > TEAM_TEMPLATE_PERSONA_MAX:
                    raise TeamError(
                        f"成员「{member_name or name}」的人设过长"
                        f"（最多 {TEAM_TEMPLATE_PERSONA_MAX} 字符）"
                    )
                spec = TeamMemberSpec(
                    name=member_name,
                    provider_name=name,
                    model=model,
                    persona=persona,
                    context_limit_tokens=self._team_provider_context_limit(name),
                )
                try:
                    provider = self._build_member_provider(name, model)
                    spec.provider = provider
                    if not spec.model:
                        spec.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 构建失败降级为错误成员
                    spec.build_error = str(e)[:200]
                specs.append(spec)
        else:
            for ms in self._resolve_members(None, chair_answers=False):
                specs.append(TeamMemberSpec(
                    provider_name=ms.provider_name, model=ms.model,
                    provider=ms.provider, build_error=ms.build_error,
                    context_limit_tokens=self._team_provider_context_limit(ms.provider_name),
                ))
        return specs

    # ---- 协作模板：成员实例化 / 工具表裁剪 / 运行级授权（本期）----

    def _template_member_specs(
        self, roles: list[dict], chair_key: tuple[str, str] = ("", ""),
        limit: int = 0,
    ) -> list[MemberSpec]:
        """模板角色 → 圆桌 MemberSpec（按角色顺序，不走去重）。

        指定服务的角色新建 Provider；留空的角色按角色顺序从「已配好 Key 的
        服务」池里轮转补位（与团队模板同口径）。同一模型承担多个角色是允许的
        ——角色靠人设（system prompt）区分，不是身份预设（role 留空，避免
        MEMBER_ROLES 的内置身份提示词被再拼一遍）。
        """
        specs: list[MemberSpec] = []
        auto: list[MemberSpec] | None = None
        cursor = 0
        for role in roles or []:
            if not isinstance(role, dict):
                continue
            name = str(role.get("name") or "").strip()
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            prompt = str(role.get("prompt") or "").strip()
            if provider_name:
                try:
                    provider = self._build_member_provider(provider_name, model)
                    specs.append(MemberSpec(
                        provider_name=provider_name,
                        model=model or getattr(provider, "model", ""),
                        provider=provider,
                        role="", name=name, prompt=prompt,
                    ))
                except Exception as e:  # noqa: BLE001 - 构建失败降级错误成员卡
                    specs.append(MemberSpec(
                        provider_name=provider_name, model=model,
                        build_error=str(e)[:200],
                        role="", name=name, prompt=prompt,
                    ))
                continue
            if auto is None:
                auto = self._resolve_members(
                    None, False, chair_key=chair_key, limit=limit,
                )
            if not auto:
                specs.append(MemberSpec(
                    provider_name="", model="",
                    build_error="未指定模型且没有已配置 Key 的服务",
                    role="", name=name, prompt=prompt,
                ))
                continue
            src = auto[cursor % len(auto)]
            cursor += 1
            specs.append(MemberSpec(
                provider_name=src.provider_name, model=src.model,
                provider=src.provider, build_error=src.build_error,
                role="", name=name, prompt=prompt,
            ))
        return specs

    def _expand_auto_pool(self, auto: list[MemberSpec]) -> list[MemberSpec]:
        """把自动池按「服务 × 配置的 models 列表」展开成补位池。

        自动池每个服务只贡献当前模型一条；团队模板的多个角色（provider 全空、
        自动挑选）从这样的池轮转补位，(服务, 模型) 键全部相同，随即被去重
        合并成一名队员——单 Key 配了多模型（如一个网关挂多个上游）时三角色
        小组会缩成一人。展开后角色尽量各得一个独立键：第一个角色仍拿当前
        模型（行为与旧自动池前段一致），后续角色依次取同服务的其余 models；
        某个 model 构建失败降级为 build_error 成员，不阻断建队。
        """
        pool: list[MemberSpec] = []
        seen: set[tuple[str, str]] = set()
        for base in auto or []:
            if not base.provider_name:
                continue
            models: list[str] = []
            pc = self.cfg.providers.get(base.provider_name)
            if pc is not None and pc.models:
                models = [str(m) for m in pc.models if str(m).strip()]
            if base.model and base.model not in models:
                models.insert(0, base.model)
            for m in models:
                key = (base.provider_name, m)
                if key in seen:
                    continue
                seen.add(key)
                if m == base.model and base.provider is not None:
                    # 当前模型条目直接复用自动池已构建的 provider 实例：
                    # 重建会多消耗一次构建（测试注入的脚本池因此整体错位）
                    pool.append(MemberSpec(
                        provider_name=base.provider_name, model=m,
                        provider=base.provider,
                    ))
                    continue
                spec = MemberSpec(provider_name=base.provider_name, model=m)
                try:
                    provider = self._build_member_provider(base.provider_name, m)
                    spec.provider = provider
                    if not spec.model:
                        spec.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 展开失败降级，不阻断建队
                    spec.build_error = str(e)[:200]
                pool.append(spec)
        return pool or list(auto or [])

    def _template_team_specs(self, template: dict) -> list[TeamMemberSpec]:
        """团队模板 → TeamMemberSpec：先补 model 再去重（与 _resolve_team_members
        同口径），被（服务, 模型）重复合并掉的角色由调用方发 Notice。

        模板的 provider 全空是常态（内置「团队·交付小组」就是）：此时实际
        入册人数受已配置 Key 的服务数限制，重复角色在这里合并，而不是建队时
        静默截断。
        """
        roles = (template or {}).get("roles") or []
        auto: list[MemberSpec] | None = None
        cursor = 0
        specs: list[TeamMemberSpec] = []
        seen: set[tuple[str, str]] = set()
        for role in roles:
            if not isinstance(role, dict):
                continue
            name = str(role.get("name") or "").strip()
            persona = str(role.get("prompt") or "").strip()
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            if provider_name:
                spec = TeamMemberSpec(
                    name=name, provider_name=provider_name, model=model,
                    persona=persona,
                    context_limit_tokens=self._team_provider_context_limit(provider_name),
                )
                try:
                    provider = self._build_member_provider(provider_name, model)
                    spec.provider = provider
                    if not spec.model:
                        spec.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 构建失败降级错误成员
                    spec.build_error = str(e)[:200]
            else:
                if auto is None:
                    auto = self._resolve_members(None, False, chair_key=("", ""))
                    # 单 Key 多模型（服务配了 models 列表）时按「服务 × 模型」展开
                    # 补位池：角色尽量各得一个独立（服务, 模型）键，少被去重合并。
                    # 展开序 = 各服务当前模型在前、其余 models 依次在后，只有
                    # 单模型服务时与旧自动池逐项一致。
                    auto = self._expand_auto_pool(auto)
                if not auto:
                    spec = TeamMemberSpec(
                        name=name, persona=persona,
                        build_error="未指定模型且没有已配置 Key 的服务",
                    )
                else:
                    src = auto[cursor % len(auto)]
                    cursor += 1
                    spec = TeamMemberSpec(
                        name=name, provider_name=src.provider_name, model=src.model,
                        provider=src.provider, build_error=src.build_error,
                        persona=persona,
                        context_limit_tokens=self._team_provider_context_limit(
                            src.provider_name
                        ),
                    )
            key = (spec.provider_name, spec.model)
            if key in seen:
                continue  # 与 team.py 的 (服务, 模型) 同键去重一致（含空键）
            seen.add(key)
            specs.append(spec)
        return specs

    @staticmethod
    def _template_allowed_tools(template: dict) -> list[str]:
        """模板声明的具名工具（run_command 走前缀通道，不进 allowed_tools）。"""
        names: list[str] = []
        for entry in effective_tool_entries((template or {}).get("roles") or []):
            base = entry.split(":", 1)[0]
            if base == _RUN_COMMAND:
                continue
            if base not in names:
                names.append(base)
        return names

    def _team_registry_for_template(
        self, full: ToolRegistry, template: dict,
    ) -> ToolRegistry:
        """按模板的 team 级工具并集 + needs 上限裁剪执行型工具表。

        这是**可见性**筛选，不是安全边界（团队每个成员共用同一 registry，
        本期不做逐成员隔离）：真正的放行/拦截仍由 GrantedGate / 会话门判定。
        needs=read 只留 READONLY；needs=write 只留 READONLY + WRITE；
        needs=exec 才可能把 DANGEROUS 工具挂上（run_command 仍只在命令前缀
        命中时由 GrantedGate 放行，delete_file 等永远逐次确认）。
        """
        needs = derive_needs((template or {}).get("roles") or [])
        entries = effective_tool_entries((template or {}).get("roles") or [])
        names = [entry.split(":", 1)[0] for entry in entries]
        subset = full.subset(names)
        if needs == "read":
            return ToolRegistry(
                [t for t in subset.all() if t.safety == Safety.READONLY]
            )
        if needs == "write":
            return ToolRegistry(
                [t for t in subset.all()
                 if t.safety in (Safety.READONLY, Safety.WRITE)]
            )
        return subset

    def _team_budget_check(
        self, run_budget: RunBudget | None = None,
        run_tokens: dict | None = None,
    ) -> Callable[[], bool]:
        """团队的预算越线判定钩子（二期）：接既有每日 token 预算护栏路径。

        与 send() 入口的护栏同口径：cfg.daily_token_budget > 0 且当日
        usage_log 汇总（store.usage_today）已达上限即「越线」；演示模式不产生
        真实费用，豁免同既有判定。读不到用量（usage_today 回落 None，2026-11
        except 审计 #9 取严口径下护栏/团队钩子仍按放行）按未越线放行——坏掉
        的仪表不该停掉团队，费用护栏由宿主其余路径兜底（编排器对钩子异常同样
        按未越线处理，这里不吞第二次）。异步实现：usage_today 是 async，编排器的
        _budget_over 对 awaitable 返回值照常等待。

        模板预算（本期）：run_budget 非空时先把跨轮累计的 token（run_tokens
        同一个 dict，挂在编排器上跨轮复用）回填进去，再判 calls/tokens/seconds
        三线；模板预算与每日预算任一越线都算越线。团队只在三个推进点检查
        （成员轮/总管轮/自动循环），不宣称成员轮中途熔断。
        """

        async def check() -> bool:
            if run_budget is not None:
                if run_tokens is not None:
                    run_budget.add_tokens(int(run_tokens.get("total", 0) or 0))
                if run_budget.reason():
                    return True
            budget = int(self.cfg.daily_token_budget or 0)
            if budget <= 0:
                return False
            if getattr(self.provider, "demo_mode", False):
                return False
            used_today = await self.store.usage_today()
            # None = 用量库读不到（审计 #9 取严口径下护栏/团队钩子按放行）
            return used_today is not None and used_today >= budget

        return check

    def _team_message_sink(self, sid: str) -> TeamMessageSink:
        """团队频道消息落库钩子（三期）：接到当前项目库的 team_messages 表。

        编排器在每条频道消息定稿后回调（team.py 的 _persist_message，含被
        停止的取消收尾路径）；每会话一个闭包，session_id 在建队时定死。store
        在回调时才取（晚绑定：测试经 _store_override 注入的实例、setup 的
        重建都能取到当前那一个）。落库是 best-effort 旁路：失败记 obs 警告
        不外溢（编排器对 sink 异常同样只记日志）——不能让落库问题拖垮团队
        轮；被用户停止取消打断的那次按容忍丢失处理（CancelledError 原样
        上抛，不吞取消）。
        """

        async def sink(team_id: str, msg: ChannelMessage) -> None:
            store = self.store
            if store is None:
                return
            try:
                await store.add_team_message(
                    team_id, seq=msg.seq, from_member=msg.from_member,
                    to_member=msg.to_member, msg_kind=msg.msg_kind,
                    task_ref=msg.task_ref, text=msg.text,
                    session_id=sid, created_at=msg.ts,
                )
            except Exception as e:  # noqa: BLE001 - 落库失败不拖垮团队轮
                obs_warning(
                    "team", "team message persist failed",
                    session_id=sid, team_id=team_id, seq=msg.seq,
                    error=str(e)[:200],
                )

        return sink

    async def _team_create(
        self, sid: str, emit: EmitFn, members_params: list | None, recorder: ChangeRecorder,
        gate: PermissionGate | None = None, working_dir: Path | None = None,
        director_mode: str = "user", director_params: dict | None = None,
        template_name: str = "",
        template: dict | None = None, grant: dict | None = None,
        collab_tokens: dict | None = None,
    ) -> TeamOrchestrator:
        """为本会话建队（chat.send team=true 且无活动团队）：发 TeamStarted 并登记。

        emit 是编排器形状的事件回调（收 pydantic 事件，_team_body 传入的
        team_emit 已带用量归属）。成员 Agent 的工具表用本会话同款全量注册表
        （改动记录进同一 recorder，队员写的文件照常进会话检查点）；gate /
        working_dir 由调用点传会话自己的（_team_body 传 runtime 的 agent.gate
        与 agent.working_dir）：权限门注入会话既有门——会话级权限档覆盖更严时
        队员同步更严，不回退引擎默认档；工作目录用会话自己的（快聊 / 跨项目
        分屏列的团队落在本会话目录里干活，不借引擎当前目录）。编排器经
        TeamMemberGate 只加「来自队员」来源标注、判定零改变（team.py 的
        TeamMemberGate 契约）。gate 缺省 None 按编排器 fail-closed 降级为
        全员顾问型（没有执行工具表），不构成第二套判定。
        成员 Agent 另透传本会话 id（写租约归属）与用户钩子 / Mods 拦截——
        对队员与主会话同款生效，都只能收紧。

        director_mode="ai"（二期）：director_params 指定担任总管的
        {provider, model}，用 _build_member_provider 独立实例化（缺 Key 等
        构建失败降级 provider=None + build_error，不阻断建队——首个总管轮
        触达时按失败隔离发注记，团队保持活动等插话重试/接管/收队）；总管
        未指定（provider 名为空）按 fail-closed 在建队期报 TeamError。预算
        越线判定注入 _team_budget_check（越线强制交付 budget_exhausted）。
        频道消息落库（三期）注入 _team_message_sink：每条定稿消息进项目库
        team_messages 表，team_id 进快照随 meta 落库（team.log 凭它回放）。
        template_name（四期常驻）：建队来源的模板名（前端「从模板创建」带上），
        记进编排器快照，供 team.resident_set 把队伍绑回模板（定时触发按
        模板匹配复用的依据）；手选成员建队传空串。
        """
        tpl = template if isinstance(template, dict) else None
        tpl_agg = (tpl or {}).get("aggregator") or {}
        grant_info = grant if isinstance(grant, dict) else {}
        read_only = bool(grant_info.get("read_only"))
        grant_scope = str(grant_info.get("scope") or "ask_each")
        if grant_scope not in ("run", "ask_each"):
            grant_scope = "ask_each"
        run_budget = RunBudget(**((tpl or {}).get("budget") or {})) if tpl is not None else None
        tokens = collab_tokens if isinstance(collab_tokens, dict) else {"total": 0}

        # 总管形态：模板 aggregator.provider 非空 = AI 总管（独立 temp provider），
        # 模板自己的收口配置优先；模板没配收口时显式传入的 director_mode="ai"
        # 仍然生效——此前无条件被覆盖成 "user"，显式参数静默吞掉、无任何提示。
        # 两者都没有才是用户总管（模板明确「没人替你验收」）。
        director: DirectorSpec | None = None
        effective_director_mode = director_mode

        def _build_director(d_provider: str, d_model: str) -> DirectorSpec:
            spec = DirectorSpec(
                provider_name=d_provider, model=d_model,
                context_limit_tokens=self._team_provider_context_limit(d_provider),
            )
            try:
                provider = self._build_member_provider(d_provider, d_model)
                spec.provider = provider
                if not spec.model:
                    spec.model = getattr(provider, "model", "")
            except Exception as e:  # noqa: BLE001 - 构建失败降级，不阻断建队
                spec.build_error = str(e)[:200]
            return spec

        if tpl is not None:
            d_provider = str(tpl_agg.get("provider") or "").strip()
            d_model = str(tpl_agg.get("model") or "").strip()
            if d_provider:
                effective_director_mode = "ai"
                director = DirectorSpec(
                    provider_name=d_provider, model=d_model,
                    context_limit_tokens=self._team_provider_context_limit(d_provider),
                    system_prompt=str(tpl_agg.get("prompt") or "").strip(),
                )
                try:
                    provider = self._build_member_provider(d_provider, d_model)
                    director.provider = provider
                    if not director.model:
                        director.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 构建失败降级，不阻断建队
                    director.build_error = str(e)[:200]
            elif director_mode == "ai":
                d_provider = str((director_params or {}).get("provider", "") or "").strip()
                d_model = str((director_params or {}).get("model", "") or "").strip()
                if not d_provider:
                    raise TeamError(
                        'director_mode="ai" 需要指定担任总管的模型服务（director.provider）'
                    )
                director = _build_director(d_provider, d_model)
                effective_director_mode = "ai"
        elif director_mode == "ai":
            d_provider = str((director_params or {}).get("provider", "") or "").strip()
            d_model = str((director_params or {}).get("model", "") or "").strip()
            if not d_provider:
                raise TeamError(
                    'director_mode="ai" 需要指定担任总管的模型服务（director.provider）'
                )
            director = _build_director(d_provider, d_model)
        specs = (
            self._template_team_specs(tpl)
            if tpl is not None else self._resolve_team_members(members_params)
        )
        if tpl is not None:
            declared = len([r for r in (tpl.get("roles") or []) if isinstance(r, dict)])
            if len(specs) < declared:
                await emit(NoticeEvent(
                    message=f"模板的 {declared} 个角色去重后组成 "
                            f"{len(specs)} 名队员：有的服务没配 Key，或角色重复"
                ))
        # 模板 max_calls 是 provider 调用次数（含重试/格式纠错），不是轮次：
        # 给每个成员与 AI 总管的 provider 套一层计数代理，团队链路（含小会）
        # 的每次真实 stream 都会 spend()（审查 finding 15）。
        if run_budget is not None:
            for spec in specs:
                if spec.provider is not None:
                    spec.provider = BudgetedProvider(spec.provider, run_budget)
            if director is not None and director.provider is not None:
                director.provider = BudgetedProvider(director.provider, run_budget)
        full_registry = self._build_full_registry(recorder)
        registry = full_registry
        if tpl is not None:
            registry = self._team_registry_for_template(full_registry, tpl)
            if read_only:
                # 「先只读跑一遍」：工具表只留 READONLY（不挂写/执行），且不授予
                # 任何具名写工具或命令前缀
                registry = ToolRegistry(
                    [t for t in full_registry.all() if t.safety == Safety.READONLY]
                )
        # 运行级授权（grant.scope=run）：只包一层 GrantedGate——写工具限工作
        # 目录内、命令限模板声明的前缀；删除语义 / 引擎主目录 / 目录外写入 /
        # DANGEROUS 一律回退会话门逐次确认。成员 Agent 在建队时缓存 gate，
        # 授权范围因此按团队生命周期固定（不做逐轮变更）。
        team_gate = gate
        if gate is not None and (read_only or grant_scope == "run"):
            if read_only:
                team_gate = GrantedGate(
                    gate, allowed_tools=(), command_prefixes=(),
                    run_id=uuid4().hex[:12],
                    label=(
                        f"模板「{tpl.get('name', '')}」只读运行"
                        if tpl is not None else "团队只读运行"
                    ),
                )
            elif grant_scope == "run":
                if tpl is not None:
                    team_gate = GrantedGate(
                        gate,
                        allowed_tools=self._template_allowed_tools(tpl),
                        command_prefixes=collect_command_prefixes(tpl.get("roles") or []),
                        run_id=uuid4().hex[:12],
                        label=f"模板「{tpl.get('name', '')}」",
                    )
                else:
                    # team=true 直发（无模板声明）：按「本次运行免确认」的同一
                    # 安全边界放行全部常规写工具；GrantedGate 仍收口——删除
                    # 语义 / 引擎主目录 / 工作目录外 / DANGEROUS 一律回退会话门
                    # 逐次确认。命令不预授权（没有模板声明的前缀清单可依）。
                    team_gate = GrantedGate(
                        gate,
                        allowed_tools=[
                            t.name for t in full_registry.all()
                            if t.safety == Safety.WRITE
                        ],
                        command_prefixes=(),
                        run_id=uuid4().hex[:12],
                        label="团队（显式成员）",
                    )
        if tpl is not None and read_only:
            await emit(NoticeEvent(
                message="本次按「只读」运行模板：队员只挂只读工具，不写文件、不运行命令。"
            ))
        team_cfg = (
            TeamCfgView(self.cfg.team, run_budget)
            if run_budget is not None else self.cfg.team
        )
        budget_note = (
            (lambda: run_budget.reason_label()) if run_budget is not None else None
        )
        orch_kwargs = dict(
            emit=emit,
            working_dir=working_dir,
            registry=registry,
            gate=team_gate,
            cfg=team_cfg,
            max_iterations=self.cfg.subagent_max_iterations,
            restrict_to_workdir=self.cfg.restrict_to_workdir,
            session_id=sid,
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
            hooks=self.hooks,
            mods=self.mods,
            director_mode=effective_director_mode,
            director=director,
            budget_check=self._team_budget_check(run_budget, tokens),
            context_limit_tokens=self.cfg.context_limit_tokens,
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            compaction_trigger=self.cfg.compaction_trigger,
            compaction_auto=self.cfg.compaction_auto,
            # 频道消息逐条落库（三期）：收队/重启后仍可按 team_id 回放全量
            message_sink=self._team_message_sink(sid),
            source_template=str((tpl or {}).get("name") or template_name),
            isolate_exec=bool(getattr(self.cfg.team, "isolated_tasks", False)),
        )
        # 预算越线文案：模板预算注入精确原因（次数 / 时长 / token），
        # 未带模板时团队沿用内置的 token 预算句
        if budget_note is not None:
            orch_kwargs["budget_note"] = budget_note
        orch = TeamOrchestrator(**orch_kwargs)
        # 模板预算与跨轮 token 累计挂编排器：同一支队伍的第二条消息起不再
        # 重读模板，预算线必须随团队生命周期延续
        orch.collab_budget = run_budget
        orch.collab_tokens = tokens
        await orch.create_team(specs)  # TeamStarted 在这里发出；超上限截断发 Notice
        self._teams[sid] = orch
        self._teams_interrupted.pop(sid, None)
        self._persist_team_state()
        return orch

    @staticmethod
    def _team_meta(orch: TeamOrchestrator, template_name: str = "") -> dict:
        """assistant 消息的团队 meta：{"mode": "team", "team": {…}}，仿 roundtable meta。

        Message 的元数据槽只有 roundtable 一个（messages.py 未设通用 meta 字段），
        团队摘要搭这个槽随会话落库/下发，前端按 mode == "team" 与圆桌卡分流。
        team 块自带建队分配的 team_id（三期）：前端回放全量频道消息
        （team.log）就凭 meta 里这个 id，收队/重启后仍然可用。

        模板身份（本期）同时放在顶层 template / template_kind：卡头与历史回放
        不依赖 team 快照的字段演进（快照里的 template 是常驻绑定的同一来源）。
        """
        meta: dict = {"mode": "team", "team": orch.snapshot()["team"]}
        name = str(template_name or getattr(orch, "source_template", "") or "").strip()
        if name:
            meta["template"] = name
            meta["template_kind"] = "team"
        return meta

    async def _team_body(
        self, text: str, emit: EmitFn, members_params: list | None,
        agent: Agent, sid: str, images: list[ImageBlock] | None = None,
        allow_create: bool = False, recorder: ChangeRecorder | None = None,
        plan_mode: bool = False,
        director_mode: str = "user", director: dict | None = None,
        template_name: str = "",
        template: dict | None = None, grant: dict | None = None,
        local: bool = True,
    ) -> dict:
        """团队轮主体：用户消息进团队频道 → 总管推进（成员唤醒 / 自动闭环）。

        - 该会话已有活动团队：消息按「@成员名」定向 / 广播进频道；
        - 没有活动团队且 allow_create：先建队（TeamStarted）再进频道。

        总管形态（二期）：user（默认）走一期用户总管——被 @ 点名的成员逐个
        唤醒；ai 走自动闭环——消息经 run_auto_turn 驱动总管（拆解 → 派工 →
        验收 → 交付）至空闲或终态；循环推进中（loop_running，渠道入站等不占
        运行位的消息源会撞上）改走 inject_user_message：只进频道并排进总管
        下一轮的最高优先级段，不重复驱动循环（在飞轮任务负责推进）。

        规划模式对团队轮同样生效（plan_mode 本轮值）：激活时成员一律按只读
        工具表唤醒（即使名下有执行型工单，set_plan_mode 现筛只读表），并发
        Notice 说明——「本轮不执行任何写操作」的用户契约不因协作模式破例。

        成员/总管发言以 TeamMessageDelta 流式下发（StreamDeltaMerger 按
        member_index + seq 分桶合并，member_index=-1 即总管），定稿
        TeamMessage；转发的工具调用 / 权限请求等过程事件是非增量，merger
        自动先冲刷缓冲再发出，顺序与普通轮一致。取消语义：编排器把半截发言
        shield 定稿进频道后原样上抛，这里补齐用量入账与 meta 落库再交还外层
        收口为 stopped。

        返回随轮次结果回传前端的团队元数据（同时作为 assistant 消息 meta 落库）。
        """
        orch = self._teams.get(sid)

        emit_ev = _dump_emit(emit)

        # 远程驱动已有团队：GrantedGate 是建队时按「本机用户点了本次运行免
        # 确认」发的，不能由后续远程消息继续搭乘（审查 finding 6）。永久
        # 收回为内部门逐次确认（fail-closed；本机要恢复免确认需重新建队）。
        if orch is not None and not local:
            team_gate = getattr(orch, "_gate", None)
            if isinstance(team_gate, GrantedGate):
                orch.set_gate(object.__getattribute__(team_gate, "_inner"))
                await emit_ev(NoticeEvent(
                    message="本次由远程连接操作该团队，模板的免确认授权已收回，"
                            "后续操作改为逐次确认。"
                ))

        # 用量按事件流归属：usage 事件先于其定稿 TeamMessage 到达，攒到定稿时
        # 记到名下（MemberTurnResult 的用量不随返回值交给宿主，事件流是逐成员
        # 归属的唯一来源）。总管轮与小会主席的 usage 先于 from="director" 的
        # 定稿到达——冲进 director_acc，行键 provider/model 取 director_info
        # （_flush_team_usage），不与队员混账。极端取消时序下剩余用量挂给最后
        # 定稿的归属方，不凭空丢账。
        # 模板预算：创建轮从模板构造；同一支队伍后续消息复用编排器上挂着的
        # 那份（预算线随团队生命周期延续，不因每轮新建而重置）。token 计数用
        # 同一个 dict 跨轮累计，并回填进 RunBudget（add_tokens 取 max，
        # 重复回填不重复累加）。
        run_tokens = getattr(orch, "collab_tokens", None) if orch is not None else None
        if not isinstance(run_tokens, dict):
            run_tokens = {"total": 0}
        run_budget = getattr(orch, "collab_budget", None) if orch is not None else None

        usage_acc: dict[str, list[int]] = {}  # 成员名 -> [input, output]
        director_acc = [0, 0]  # 总管（AI 模式）累计 [input, output]
        pending_usage: list = []

        async def team_emit(ev) -> None:
            if ev.kind == "usage":
                pending_usage.append(ev)
                # 模板预算的 token 线吃**全部** provider 用量（成员 + 总管 +
                # 小会主席），不依赖定稿归属结算的时序——预算检查点随时能读到
                run_tokens["total"] = int(run_tokens.get("total", 0) or 0) + int(
                    ev.input_tokens
                ) + int(ev.output_tokens)
            elif ev.kind == "team_message" and ev.from_member == "director":
                if orch is not None and orch.director_mode == "ai":
                    # 总管轮与小会主席的 usage 先于 from="director" 的定稿到达
                    # ——冲进 director_acc，行键 provider/model 取 director_info
                    # （_flush_team_usage），不与队员混账。仅在 AI 总管模式生效：
                    # user 模式的 director 消息是系统代发的派工/验收话术（没有
                    # 总管模型），不冲账——窗口内的成员用量留给成员自己的定稿或
                    # 轮末 _flush_team_usage 按最后归属结算，不结出 provider=""
                    # /model="" 的幽灵总管账、成员名下不丢账。
                    for u in pending_usage:
                        director_acc[0] += int(u.input_tokens)
                        director_acc[1] += int(u.output_tokens)
                    pending_usage.clear()
            elif (ev.kind == "team_message"
                  and ev.from_member not in ("user", "system", "director")):
                bucket = usage_acc.setdefault(ev.from_member, [0, 0])
                for u in pending_usage:
                    bucket[0] += int(u.input_tokens)
                    bucket[1] += int(u.output_tokens)
                pending_usage.clear()
            elif (ev.kind == "team_message" and ev.from_member == "system"
                  and pending_usage and orch is not None and orch.director_mode == "ai"):
                # 小会融合失败只有 from=system 的注记收尾（没有裁定定稿冲账）：
                # 此时未冲账的用量只可能是小会主席的——成员/总管轮的用量都随
                # 各自定稿消息冲账。主动结算进总管账，别让轮末兜底把主席的
                # token 记到队尾成员头上（与 _director_usage/snapshot 失协）。
                for u in pending_usage:
                    director_acc[0] += int(u.input_tokens)
                    director_acc[1] += int(u.output_tokens)
                pending_usage.clear()
            await emit_ev(ev)

        t0 = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        await emit_ev(TurnStarted(iteration=1))
        # 规划模式对团队轮同样生效（一期评审遗留）：本轮值同步给编排器（建队
        # 路径在下方建完再同步），激活时并发 Notice 说明只读执行——每轮都发，
        # 团队进行中切换规划模式的行为对用户始终可见。
        if orch is not None:
            orch.set_plan_mode(plan_mode)
        if plan_mode:
            await emit_ev(NoticeEvent(
                message="规划模式已开启：本轮队员以只读方式执行（不写文件、不运行命令），"
                        "只做调研与汇报；需要队员动手请切回执行模式后再发消息。"
            ))
        # 用户消息先入主历史（与圆桌同姿态）：团队期间的问题不丢，收队恢复
        # 普通回合后后续轮仍有上下文；图片随消息保留（队员不看图）。
        agent.history.append(Message.user(text, images))

        try:
            if orch is None:
                if not allow_create:
                    # 理论不可达（pipeline 入口保证 team 或活动团队至少一个成立）
                    raise RuntimeError("当前会话没有进行中的团队")
                orch = await self._team_create(
                    sid, team_emit, members_params, recorder or ChangeRecorder(),
                    # 会话私有门与会话自己的工作目录（本轮 agent 即 runtime.agent）：
                    # 队员判定跟会话走，不回退引擎默认档（会话档更严时队员同步更严）
                    gate=agent.gate, working_dir=agent.working_dir,
                    director_mode=director_mode, director_params=director,
                    template_name=template_name,
                    template=template, grant=grant,
                    collab_tokens=run_tokens,
                )
                # _team_create 内已经建好并被预算钩子引用的那一份才是执法
                # 实例：这里绝不能再用本轮临时构造的 run_budget 覆盖它，
                # 否则 orch.collab_budget 的 calls/tokens 永远是 0，meta 也
                # 读不到越线原因（审查 finding 16）。
                orch.collab_tokens = run_tokens
                run_budget = orch.collab_budget
                orch.set_plan_mode(plan_mode)
            else:
                # 逐轮重绑发射链（仅空闲轮）：编排器只在建队轮拿到过 emit，
                # 第二条消息起事件与用量会流进建队轮早已收尾的旧闭包——前端因
                # 同连接碰巧可见，usage_log 的归属却静默丢失。本轮要驱动编排器
                # （唤醒/自动循环），事件与用量就必须挂本轮的 team_emit。
                # 插话路径例外（loop_running=True，在飞轮正持有发射链）：重绑
                # 会把在飞轮的用量挪进本轮早已冲刷完的账本——保持不重绑。
                if not orch.loop_running:
                    orch._emit = team_emit
            if orch.director_mode == "ai":
                # 总管不挂文件工具：用户点名的项目文件在这里只读预取进总管
                # 下一轮唤醒（与圆桌/对抗同一套 _preload_mentioned_files）。
                # 不进频道、不落库——频道里仍是用户原文，队员看不到快照。
                director_context = ""
                if text:
                    try:
                        director_context = self._preload_mentioned_files(
                            text, agent.history, getattr(agent, "working_dir", None)
                        )
                    except Exception:  # noqa: BLE001 - 预取失败不拦团队轮
                        director_context = ""
                if director_context:
                    await emit_ev(NoticeEvent(message=(
                        "团队总管没有文件工具：已把你点名的项目文件（只读）带给总管，"
                        "最多 3 个，过长会截取；频道里仍是你的原文。"
                    )))
                if orch.loop_running:
                    # 循环推进中的插话：只进频道并排进总管下一轮（最高优先级段），
                    # 不重复驱动循环。若 inject 返回时循环恰好已收束（竞态窗口），
                    # 插话留在待注入清单里，随本会话下一条消息一并处理
                    result = await orch.inject_user_message(
                        text, images=images, director_context=director_context,
                    )
                else:
                    result = await orch.run_auto_turn(
                        text, images=images, director_context=director_context,
                    )
            else:
                result = await orch.handle_user_message(text, images=images)
        except asyncio.CancelledError:
            # 用户停止：半截发言已在编排器里 shield 定稿进频道（seq 复用无空洞）。
            # 这里把已花费的用量入账、meta 与纪要落进主历史，再上抛交给 pipeline
            # 的 stopped 收尾（冲刷合并缓冲 + 落库）。
            await self._flush_team_usage(
                sid, orch, usage_acc, pending_usage, director_acc=director_acc,
            )
            if orch is not None:
                stopped_msg = Message.assistant([TextBlock(
                    text="（本轮已停止：正在发言的队员已把已产出的部分定稿进团队频道。）",
                )])
                stopped_msg.roundtable = self._team_meta(
                    orch,
                    str((template or {}).get("name") or template_name or ""),
                )
                agent.history.append(stopped_msg)
                for ev in (
                    AssistantMessage(message=stopped_msg.model_dump()),
                    TurnFinished(stop_reason="cancelled", iterations=1, duration_ms=_ms()),
                ):
                    try:
                        await emit_ev(ev)
                    except asyncio.CancelledError:
                        pass  # 收尾帧发不出就算了：历史与落库不丢
            raise
        except TeamError:
            # 参数/状态非法（如解析不出任何队员）：与圆桌同姿态补 TurnFinished(error)
            # 让前端轮次状态收口，错误原文上抛给 WS 层。若团队已在轮中进终态
            # （轮次耗尽/预算越线等），用量照常结算、活动登记照常摘除——否则
            # 已收队的团队永远挂在 _teams 里，该会话此后每条消息都报「当前
            # 没有进行中的团队」，只能手动收队解困。
            await self._flush_team_usage(
                sid, orch, usage_acc, pending_usage, director_acc=director_acc,
            )
            if orch is not None and orch.finished is not None:
                if self._teams.pop(sid, None) is not None:
                    self._release_team_providers(orch)
                self._teams_interrupted.pop(sid, None)
                self._persist_team_state()
            await emit_ev(TurnFinished(stop_reason="error", iterations=1, duration_ms=_ms()))
            raise

        await self._flush_team_usage(
            sid, orch, usage_acc, pending_usage, director_acc=director_acc,
        )
        finished = result.get("finished")
        if finished is not None:
            # 终态（交付确认 / 收队 / 轮次耗尽）：团队关闭，会话恢复普通回合
            if self._teams.pop(sid, None) is not None:
                self._release_team_providers(orch)
            self._teams_interrupted.pop(sid, None)
            self._persist_team_state()

        meta = self._team_meta(
            orch, str((template or {}).get("name") or template_name or "")
        )
        if run_budget is not None and run_budget.reason():
            meta["budget_exceeded"] = run_budget.reason()
            meta["budget_note"] = run_budget.reason_label()
        digest = Message.assistant([TextBlock(text=_team_digest(orch, result))])
        digest.roundtable = meta
        agent.history.append(digest)
        await emit_ev(AssistantMessage(message=digest.model_dump()))
        await emit_ev(TurnFinished(stop_reason="end_turn", iterations=1, duration_ms=_ms()))
        return meta

    async def _flush_team_usage(
        self, sid: str | None, orch: TeamOrchestrator | None,
        usage_acc: dict[str, list[int]], pending_usage: list,
        director_acc: list[int] | None = None,
    ) -> None:
        """团队轮的用量入账 usage_log（归属当前会话，仿圆桌 usage_rows）。

        逐成员各一行；AI 总管模式另结一行挂总管名下（provider/model 取
        director_info，不挂在任何队员名下——总管轮与小会主席的用量经
        team_emit 的 from="director" 分支攒进 director_acc）。
        """
        if orch is None or not sid:
            return
        if pending_usage:
            bucket: list[int] | None = None
            if usage_acc:
                last = next(reversed(usage_acc))
                bucket = usage_acc[last]
            elif director_acc is not None and orch.director_mode == "ai":
                # AI 模式下还没有任何成员定稿过：剩余用量多半是被取消的总管轮的
                bucket = director_acc
            if bucket is not None:
                for u in pending_usage:
                    bucket[0] += int(u.input_tokens)
                    bucket[1] += int(u.output_tokens)
                pending_usage.clear()
        rows = [
            (spec.provider_name, spec.model, acc[0], acc[1])
            for spec in orch.roster
            if (acc := usage_acc.get(spec.name)) and (acc[0] or acc[1])
        ]
        if director_acc is not None and (director_acc[0] or director_acc[1]):
            info = orch.director_info()
            rows.append((info["provider"], info["model"], director_acc[0], director_acc[1]))
        if not rows:
            return
        try:
            for provider, model, tin, tout in rows:
                await self.store.add_usage(sid, provider, model, tin, tout, 0)
        except Exception:  # noqa: BLE001 - 记账失败不影响本轮结果
            pass
        # 团队烧的真金白银也要触发越线提醒（内部不抛）
        await self._check_budget_alerts()

    def _team_for(self, params: dict) -> tuple[str, TeamOrchestrator]:
        """按 params.session_id（缺省活动会话）取活动团队；没有就报参数错。

        会话归属兜底（安全审查 M03）：session_id 键存在但为空串——远程连接
        未绑定会话时由 app 层收敛注入（_team_scoped_params）——按「没有团队」
        处理，不回退全局活动会话。空串落进 ``or`` 链会重新拿到
        backend.session，全局指针切走后收敛对象随之漂移，fail closed 形同虚设。
        """
        raw = params.get("session_id")
        sid = str(raw) if raw is not None else (self.session.id if self.session else "")
        orch = self._teams.get(sid)
        if orch is None:
            raise TeamError("当前会话没有进行中的团队")
        return sid, orch

    async def _cancel_team_turn_and_settle(self, sid: str) -> None:
        """取消该会话正在跑的轮，并等它把取消收尾跑完（team.stop / team.deliver 专用）。

        时序前提（一期评审遗留）：被取消成员轮的半截发言要 shield 定稿进频道、
        team_message 帧要先于 team_finished 到达、终态 snapshot 不能缺这条发言
        ——所以先「取消 + 等收尾」（定稿 / 合并缓冲冲刷 / 落库都落地），再走
        finish() 生成快照，不靠前端幂等兜底。等待期间的再次取消吞掉（收队语义
        已定，与 pipeline 收尾的取向一致）；兜底超时不无限等——收队不能被卡死
        的轮拖住，超时记警告后照常收队。
        """
        rt = self.runtimes.get(sid)
        task = rt.run_task if rt else None
        if task is None or task.done():
            return
        self.cancel_run(sid)  # 上面的 done 检查保证这里必是活任务
        deadline = time.monotonic() + _PERSIST_HARVEST_TIMEOUT
        while not task.done() and time.monotonic() < deadline:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            except asyncio.CancelledError:
                asyncio.current_task().uncancel()  # 收队请求被再次取消：吞掉继续等
            except TimeoutError:
                pass  # 到点再看一眼：deadline 兜底，不无限等
            except Exception:  # noqa: BLE001 - 被取消轮自身的错误在它的调用链上处理
                pass
        if not task.done():
            obs_warning(
                "team",
                f"cancelled turn did not settle in time before team finish, sid={sid}",
                session_id=sid,
            )

    def team_get(self, params: dict) -> dict:
        """team.get：团队快照（名册 / 工单 / 频道尾部）。

        无活动团队时返回空态；该会话有「已中断」记录（重启/切项目前留过团队）
        时附带 interrupted 记录供前端提示。会话归属兜底见 _team_for（M03）：
        显式空串按「无会话」处理，不回退全局活动会话。
        """
        raw = params.get("session_id")
        sid = str(raw) if raw is not None else (self.session.id if self.session else "")
        orch = self._teams.get(sid)
        if orch is not None:
            team = orch.snapshot()["team"]
            # 常驻标记（T3.2）：只贴在 team.get 的回包上，不进 snapshot——
            # meta 落库形状保持不变，前端团队卡凭它点亮「常驻」开关
            team["resident"] = self.resident_teams.is_resident(orch.team_id)
            return {"active": True, "session_id": sid, "team": team}
        out: dict = {"active": False, "session_id": sid, "team": None}
        if sid in self._teams_interrupted:
            out["interrupted"] = self._teams_interrupted[sid]
        return out

    async def team_room(self, params: dict) -> dict:
        """team.room：团队室聚合探针（页签激活 / 会话切换 / 断线重连各调一次）。

        一次带回 team.get 形状的快照块 + latest_seq（频道最新消息序号，供前端
        校准未读 badge）+ stats（工单按状态计数与成员用量，供成员带与工单板
        首画免二次推断），替代「team.get + 从频道尾部推断最新 seq」的拼装。
        只读方法、零新安全语义：session_id 轴与 team.get 同一口径（非本机连接
        的 session_id 已在 WS 入口被 _team_scoped_params 收敛）；team_id 轴为
        常驻团队（T3.2）预留的主轴，一期前端不传——收到时沿用 team.log 的
        三态归属判定（活动命中 / 落库行归属 / 外来），判定不过按同文报
        TeamError。无活动团队时 active=false、team=null、latest_seq=0，有中断
        记录时附 interrupted（同 team.get 口径）。
        """
        raw = params.get("session_id")
        sid = str(raw) if raw is not None else (self.session.id if self.session else "")
        team_id = str(params.get("team_id", "") or "").strip()

        def _room_active(orch) -> dict:
            team = orch.snapshot()["team"]
            # 常驻标记（T3.2）：与 team.get 回包同款贴法——「同一快照源，
            # 页签与卡不两套」的契约对常驻开关同样成立
            team["resident"] = self.resident_teams.is_resident(orch.team_id)
            msgs = orch.channel.messages
            return {
                "active": True, "session_id": sid, "team": team,
                "latest_seq": msgs[-1].seq if msgs else 0,
                "stats": _room_stats(team),
            }

        if team_id:
            # team_id 轴（T3.2 预留）：归属判定沿用 team.log 的三态口径（§15.2
            # 逐字同款）——活动命中（活动团队 team_id 相等）或落库行归属本会话，
            # 任一命中即放行；库内存在外来行且两证皆无，按 team.log 同文报
            # TeamError；完全未知 / 未落库的 team_id 不拒绝——照 team.log 回空
            # （空结果没有可泄露的内容）。判定过了但团队不在册（已收队/重启）
            # 同样回空态——频道回放走 team.log，本方法只服务活动团队的首画。
            orch = self._teams.get(sid)
            active_hit = orch is not None and bool(orch.team_id) and orch.team_id == team_id
            rows = (
                await self.store.list_team_messages(team_id)
                if self.store is not None else []
            )
            owned = bool(sid) and any(r.get("session_id") == sid for r in rows)
            foreign = any(r.get("session_id") and r.get("session_id") != sid for r in rows)
            if not active_hit and not owned and foreign:
                raise TeamError("team_id 不属于当前会话：不能回放其他会话的团队频道")
            if not active_hit:
                return {"active": False, "session_id": sid, "team": None,
                        "latest_seq": 0, "stats": _room_empty_stats()}
            return _room_active(orch)

        out: dict = {"active": False, "session_id": sid, "team": None,
                     "latest_seq": 0, "stats": _room_empty_stats()}
        orch = self._teams.get(sid)
        if orch is not None:
            return _room_active(orch)
        if sid in self._teams_interrupted:
            out["interrupted"] = self._teams_interrupted[sid]
        return out

    async def _push_team_task_event(self, sid: str, task: dict) -> None:
        """轮外广播工单板变化（team_task_updated，含 session_id）。

        用户在工单卡上的改板（team.task_add / team.task_update）不经过任何
        轮次发射链：编排器手里那份 emit 是上一轮的旧闭包，闲置时经它发出的
        帧到不了前端——主会话团队卡靠请求响应就地更新，右侧团队室面板没有
        这条路，就停在旧状态。这里按轮内事件同形状直接广播给在线连接；与
        轮内事件重复时前端按工单编号幂等覆盖，无害。
        """
        ev = {
            "kind": "team_task_updated",
            "session_id": sid,
            "task_id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "assignee": str(task.get("assignee") or ""),
            "type": str(task.get("type") or "exec"),
            "status": str(task.get("status") or "pending"),
            "redo": int(task.get("redo") or 0),
            "detail": str(task.get("detail") or ""),
            "source": dict(task.get("source") or {}),
            "auto_dispatch": bool(task.get("auto_dispatch") or False),
        }
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(ws_emit(ev))
            except RuntimeError:
                pass

    async def team_task_add(self, params: dict) -> dict:
        """team.task_add：登记工单（待办）。指派对象必须在名册内，依赖必须存在。"""
        sid, orch = self._team_for(params)
        deps = params.get("deps")
        task = await orch.task_add(
            str(params.get("title", "")),
            str(params.get("assignee", "")),
            type=str(params.get("type") or "exec"),
            accept=str(params.get("accept", "") or ""),
            deps=[str(d) for d in deps] if isinstance(deps, list) else None,
        )
        await self._push_team_task_event(sid, task)
        return {"task": task}

    async def adversarial_to_team_tasks(self, params: dict) -> dict:
        """从已落库对抗元数据创建受控顾问工单，不接受客户端提交发现正文。

        来源校验只认服务端持久化的消息（seq + 当前会话归属）；发现的裁决必须
        是 confirmed / partial，正文与依据一律从库内 meta 取，客户端只能选 id
        与指派对象。只登记待办、不自动派工：自动循环在飞时整单拒绝，避免
        「转单即被派发」绕过用户的显式操作。
        """
        sid, orch = self._team_for(params)
        if self.store is None:
            raise TeamError("当前会话存储不可用，无法验证对抗发现来源")
        if orch.loop_running:
            raise TeamError("总管正在自动推进中：请等本轮结束后再转为工单")
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise TeamError("无效的对抗消息序号") from None
        if seq <= 0:
            raise TeamError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise TeamError("指定消息不是已保存的对抗审查结果")
        meta = message.adversarial
        if meta.get("status") != "done":
            raise TeamError("仅已完成的对抗审查可以转为工单")
        wanted = params.get("finding_ids")
        ids = list(dict.fromkeys(str(v) for v in wanted if v)) if isinstance(wanted, list) else []
        if not ids or len(ids) > 20:
            raise TeamError("请选择 1 至 20 条发现")
        selected = []
        for finding in meta.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            fid = str(finding.get("id") or "")
            if fid not in ids:
                continue
            verdict = finding.get("verdict")
            if verdict not in ("confirmed", "partial"):
                raise TeamError(f"发现 {fid} 尚未裁定成立或部分成立")
            selected.append(finding)
        if {str(f.get("id") or "") for f in selected} != set(ids):
            raise TeamError("部分发现不属于该对抗结果")
        assignee = str(params.get("assignee") or "")
        tasks = await orch.tasks_from_adversarial(seq=seq, findings=selected, assignee=assignee)
        return {"tasks": tasks}

    async def roundtable_to_team_task(self, params: dict) -> dict:
        """把已落库的圆桌融合结论登记为一张工单（P5 跨形态流转：圆桌 → 团队）。

        来源校验只认服务端持久化的消息（seq + 当前会话归属）：工单 detail=
        融合结论全文（库内 assistant 正文）、title=结论首行截断，不接受客户端
        提交的正文。对照模式没有融合结论、已取消的圆桌不受理。只登记待办、
        不派工（自动循环在飞时整单拒绝，与对抗转工单同姿态）；同一来源幂等。
        """
        sid, orch = self._team_for(params)
        if self.store is None:
            raise TeamError("当前会话存储不可用，无法验证圆桌结论来源")
        if orch.loop_running:
            raise TeamError("总管正在自动推进中：请等本轮结束后再转为工单")
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise TeamError("无效的圆桌消息序号") from None
        if seq <= 0:
            raise TeamError("缺少圆桌消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.roundtable:
            raise TeamError("指定消息不是已保存的圆桌结论")
        if message.roundtable.get("mode") == "compare":
            raise TeamError("对照模式没有融合结论，不能转为工单")
        if message.roundtable.get("status") == "cancelled":
            raise TeamError("该圆桌已取消，不能转为工单")
        text = message.text.strip()
        if not text:
            raise TeamError("该圆桌没有可用的融合结论")
        title = text.splitlines()[0].strip()[:60] or f"圆桌结论（seq {seq}）"
        task = await orch.task_from_roundtable(
            seq=seq, title=title, detail=text,
            assignee=str(params.get("assignee") or ""),
        )
        return {"task": task}

    async def team_task_update(self, params: dict) -> dict:
        """team.task_update：工单状态流转（板只由用户变更，成员不自行改板）。

        待办→进行中→待验收→完成；待验收→进行中视为打回（redo+1，超上限拒绝）；
        status="error" 走失败收尾（用户放弃，待办/进行中/待验收均可）。置为
        进行中（派工/重派/打回重做）后代发一条 director 的 assign 频道消息，
        让被派队员在频道里看到派工。
        """
        sid, orch = self._team_for(params)
        status = str(params.get("status", ""))
        task = await orch.task_update(str(params.get("task_id", "")), status)
        assign_msg = None
        if status == "in_progress":
            assign_msg = await orch.notify_assign(
                str(task["id"]), note=str(params.get("note", "") or "")
            )
        await self._push_team_task_event(sid, task)
        return {"task": task, "assign_message": assign_msg}

    async def team_stop(self, params: dict) -> dict:
        """team.stop：收队（立即终止）——TeamFinished(aborted) + 未尽事项。

        正在跑的成员轮先取消并**等收尾完成**（半截发言 shield 定稿进频道、
        team_message 帧先于 team_finished 到达，快照不缺这条发言，见
        _cancel_team_turn_and_settle）；收队后该会话恢复普通回合。
        """
        sid, orch = self._team_for(params)
        await self._cancel_team_turn_and_settle(sid)
        result = await orch.stop(reason=str(params.get("reason", "") or ""))
        if self._teams.pop(sid, None) is not None:
            self._release_team_providers(orch)
        self._persist_team_state()
        return {
            "status": result["status"],
            "summary": result["summary"],
            "session_id": sid,
            "team": result["snapshot"]["team"],
        }

    async def team_deliver(self, params: dict) -> dict:
        """team.deliver：交付（用户确认）——TeamFinished(done) + 《交付说明》。

        设计 §3 的停机条件「总管宣布交付」，一期总管是用户本人：这是全部
        工单完成后团队唯一的正常终态路径（收队恒为 aborted，轮次耗尽是
        强制交付）。未完成的工单不拦——按《交付说明》记未尽事项；正在跑的
        成员轮先取消并等收尾完成（同收队的时序前提，见
        _cancel_team_turn_and_settle），交付后该会话恢复普通回合。
        """
        sid, orch = self._team_for(params)
        await self._cancel_team_turn_and_settle(sid)
        result = await orch.finish(status="done")
        if self._teams.pop(sid, None) is not None:
            self._release_team_providers(orch)
        self._persist_team_state()
        return {
            "status": result["status"],
            "summary": result["summary"],
            "session_id": sid,
            "team": result["snapshot"]["team"],
        }

    async def team_takeover(self, params: dict) -> dict:
        """team.takeover：用户接管（二期 AI 总管）——切回用户总管模式。

        正在推进的自动闭环（总管轮/成员唤醒轮）先取消并**等收尾完成**（半截
        发言 shield 定稿进频道、team_message 帧先于后续帧到达，快照不缺这条
        发言，见 _cancel_team_turn_and_settle）；随后编排器切 director_mode
        ="user"：未消费的插话与待开小会作废、停滞标记清零，工单板与团队频道
        原样保留，成员 Agent（独立 history）全部复用——此后 handle_user_message
        照常（一期行为）。
        """
        sid, orch = self._team_for(params)
        await self._cancel_team_turn_and_settle(sid)
        result = await orch.takeover()
        self._persist_team_state()  # director_mode 变了：活动登记同步落盘
        return {
            "director_mode": result["director_mode"],
            "session_id": sid,
            "team": result["snapshot"]["team"],
        }

    # ---- 团队设置（设置 · 团队）：读回当前值 → 编辑 → 保存后热生效 ----

    def team_config_detail(self) -> dict:
        """设置页团队卡片的数据（三期，仿 roundtable_detail）。"""
        t = self.cfg.team
        return {
            "max_members": t.max_members,
            "member_timeout_s": t.member_timeout_s,
            "max_rounds": t.max_rounds,
            "redo_limit": t.redo_limit,
            "stall_limit": t.stall_limit,
            "cron_resume_limit": t.cron_resume_limit,
            "isolated_tasks": t.isolated_tasks,
            "config_hint": (
                "团队让多个模型分工协作：总管拆解目标、登记工单，队员按工单真实"
                "读写文件、运行命令（照常走权限确认），完成后交付。成员来自「模型"
                "服务」里已配好 Key 的服务，在输入框的团队面板勾选并写一句话人设；"
                "下面的上限用于防止自动循环失控。「无人值守续跑」是定时团队任务里"
                "总管空闲时自动催续跑的次数（0 = 空闲即收队）。改动对之后新建"
                "的团队生效，进行中的团队沿用建队时的值。"
            ),
        }

    async def team_config_save(self, params: dict) -> dict:
        """保存团队设置（写 config.toml 的 [team] 段）并回传新状态（clamp 仿
        roundtable_save）。热生效指此后新建的团队取新值；进行中团队的轮次
        上限等在建队时已取值，不回改。"""
        updates: dict = {}
        if params.get("max_members") is not None:
            updates["max_members"] = max(1, min(8, int(params["max_members"])))
        if params.get("member_timeout_s") is not None:
            updates["member_timeout_s"] = max(10, int(params["member_timeout_s"]))
        if params.get("max_rounds") is not None:
            updates["max_rounds"] = max(1, int(params["max_rounds"]))
        if params.get("redo_limit") is not None:
            updates["redo_limit"] = max(0, min(5, int(params["redo_limit"])))
        if params.get("stall_limit") is not None:
            updates["stall_limit"] = max(1, int(params["stall_limit"]))
        if params.get("cron_resume_limit") is not None:
            updates["cron_resume_limit"] = max(0, min(3, int(params["cron_resume_limit"])))
        if params.get("isolated_tasks") is not None:
            updates["isolated_tasks"] = bool(params.get("isolated_tasks"))
        if updates:
            update_config_section("team", updates)
            self.cfg = load_config()
        return self.team_config_detail()

    # ---- 团队模板（三期）：把一次建队配置存成可复用的具名条目 ----
    # 全局册（与会话无关，teams.json 单文件）：前端建队浮层「从模板创建」、
    # 团队卡「另存为模板」。名字/总管形态的校验在 TeamTemplateStore.upsert，
    # TeamError 原文回 WS 调用方。

    def team_template_list(self) -> dict:
        """team.template_list：全部模板（按保存顺序；member 字段见契约）。"""
        return {"templates": self.team_templates.list()}

    async def team_template_save(self, params: dict) -> dict:
        """team.template_save：保存模板。name 是唯一键——同名覆盖原条目
        （位置不变、内容换新，created_at 刷新为本次保存时刻），新名追加。
        成员行只存 (provider, model, name, persona) 标识，不校验服务存在性
        （应用模板时走 chat.send 既有解析路径，缺 Key 照常降级错误成员）。

        协作模板扩展（本期，全部可选）：needs / tools_allow / tools_deny /
        budget——缺省时同名沿用旧值（旧前端/旧调用方保存不会把已声明的授权
        面抹掉）。内置名保留（内置团队模板只在代码里、不落盘）；与协作模板册
        重名拒绝（两册的 remove/preview 都按 source 消歧）。
        """
        # 先用团队模板册自己的名称校验：空名/路径/冒号等错误按团队模板口径
        # 报（与旧接口文案一致），再跑协作扩展字段的校验
        name_raw = validate_team_template_name(str(params.get("name", "") or ""))
        if is_builtin_template_name(name_raw):
            raise TeamError(
                f"「{name_raw}」是内置模板名，不能覆盖；请改名后另存"
                f"（例如「{name_raw} · 我的版本」）"
            )
        if any(t.name == name_raw for t in self.collab_templates.templates):
            raise TeamError(f"已有同名的协作模板：{name_raw}，请换一个名字")
        old = next(
            (t for t in self.team_templates.templates if t.name == name_raw), None
        )
        raw_members = params.get("members")
        members: list[TeamTemplateMember] = []
        for m in (raw_members if isinstance(raw_members, list) else []):
            if not isinstance(m, dict):
                continue
            member_name = str(m.get("name", "") or "").strip()
            persona = str(m.get("persona", "") or "").strip()
            if len(persona) > TEAM_TEMPLATE_PERSONA_MAX:
                raise TeamError(
                    f"成员「{member_name or '未命名'}」的人设过长"
                    f"（最多 {TEAM_TEMPLATE_PERSONA_MAX} 字符）"
                )
            members.append(TeamTemplateMember(
                provider=str(m.get("provider", "") or "").strip(),
                model=str(m.get("model", "") or "").strip(),
                name=member_name,
                persona=persona,
            ))
        raw_director = params.get("director")
        # 可选协作字段：缺省沿用旧值（同名覆盖时不丢授权声明）
        needs = (
            str(params.get("needs") or "").strip()
            if params.get("needs") is not None
            else str(getattr(old, "needs", "read") or "read")
        )
        if isinstance(params.get("tools_allow"), list):
            tools_allow = [str(x) for x in params["tools_allow"]]
        else:
            tools_allow = list(getattr(old, "tools_allow", []) or [])
        if isinstance(params.get("tools_deny"), list):
            tools_deny = [str(x) for x in params["tools_deny"]]
        else:
            tools_deny = list(getattr(old, "tools_deny", []) or [])
        raw_budget = params.get("budget")
        if isinstance(raw_budget, dict):
            budget = _CollabTeamTemplateBudget(**raw_budget)
        elif old is not None and getattr(old, "budget", None) is not None:
            old_budget = old.budget
            budget = (
                old_budget.model_copy(deep=True)
                if hasattr(old_budget, "model_copy")
                else _CollabTeamTemplateBudget(**dict(old_budget))
            )
        else:
            budget = _CollabTeamTemplateBudget()
        # 复用协作模板的条目校验（needs 取值、run_command:<前缀> 的安全提炼、
        # 条目长度/逗号等）与注册表感知检查：团队模板不另起一套宽松口径
        probe = CollabTemplate(
            name=name_raw or "团队模板",
            kind="team",
            roles=[{
                "key": "team", "name": name_raw or "团队",
                "needs": needs, "tools_allow": tools_allow,
                "tools_deny": tools_deny,
            }],
            budget=budget.model_dump(),
        )
        validate_collab_template(probe, allow_builtin=False)
        role = probe.roles[0]
        warnings = self._collab_template_warnings(probe)
        template = _CollabTeamTemplate(
            name=str(params.get("name", "") or ""),
            director_mode=str(params.get("director_mode") or "user"),
            director=TeamTemplateDirector(
                provider=str((raw_director or {}).get("provider", "") or "").strip(),
                model=str((raw_director or {}).get("model", "") or "").strip(),
            ),
            members=members,
            created_at=time.time(),  # 宿主盖戳（模板册自身不感知时钟）
            needs=role.needs,
            tools_allow=list(role.tools_allow),
            tools_deny=list(role.tools_deny),
            budget=_CollabTeamTemplateBudget(**probe.budget.model_dump()),
        )
        self.team_templates.upsert(template)
        return {"template": template.model_dump(), "warnings": warnings}

    async def team_template_remove(self, params: dict) -> dict:
        """team.template_remove：按名删模板（名字精确匹配；不存在报 TeamError）。"""
        name = str(params.get("name", "") or "").strip()
        self.team_templates.remove(name)
        return {"name": name}

    # ---- 常驻团队（T3.2）：设为常驻 / 解除常驻 ----
    # 全局册（与会话无关，team_resident.json 单文件）：登记「哪支队伍跨会话
    # 常驻」与复用所需的工单板快照、频道 seq 水位。写面仅本机（与模板册同
    # 姿态，dispatch 表 local_only=True）；定时团队触发时按（模板，项目）匹配
    # 复用（automation 的 _execute_cron_team_turn），频道历史与工单板延续。

    async def team_resident_set(self, params: dict) -> dict:
        """team.resident_set：把当前会话的活动团队登记为常驻（幂等，重复设刷新）。

        template 的取值优先级：调用方显式带的 > 编排器记录的建队来源模板
        （快照 meta 的 template 键，前端「从模板创建」随 chat.send 的
        team_template 记下——UI 口径不传 template 也绑得回去，定时团队触发
        才能按（模板，项目）匹配复用）> 原登记条目的旧值。绑了模板的常驻
        队伍才会被定时团队任务按（模板，项目）匹配复用；未绑的只登记（界面
        上显示常驻，但没有定时触发面）。非空的 template 必须在模板册里存在，
        防止绑定到永不匹配的悬空名。工单板快照与频道水位取登记时刻的实时
        状态，定时复用前的最后一次刷新以最近一次运行收尾的回写为准。
        """
        sid, orch = self._team_for(params)
        if not orch.team_id:
            raise TeamError("该团队还没有分配 team_id，不能登记常驻")
        template = str(params.get("template") or "").strip() \
            or str(getattr(orch, "source_template", "") or "").strip()
        if template and is_builtin_template_name(template):
            # 内置团队模板只在代码里、不落盘：常驻/定时复用都要按名字回查模板册，
            # 给可执行的出路而不是一句「不存在」
            raise TeamError(
                f"「{template}」是内置团队模板，不能直接设为常驻；请先在 设置 · 多模型协作 里"
                "「复制」成自己的模板再设为常驻"
            )
        if template and not any(
            t.name == template for t in self.team_templates.templates
        ):
            raise TeamError(f"团队模板不存在：{template}")
        sess = await self.store.get_session(sid) if self.store is not None else None
        old = self.resident_teams.get(orch.team_id)
        entry = ResidentTeamEntry(
            team_id=orch.team_id,
            name=str((sess.title if sess else "") or ""),
            template=template or (old.template if old else ""),
            project_id=sess.project_id if sess else None,
            session_id=sid,
            director_mode=orch.director_mode,
            roster=[m.name for m in orch.roster],
            board=[t.to_dict() for t in orch.board.tasks()],
            last_seq=orch.channel.messages[-1].seq if orch.channel.messages else 0,
            wake_count=old.wake_count if old else 0,
        )
        self.resident_teams.upsert(entry)
        return {"ok": True, "team_id": orch.team_id, "resident": True,
                "entry": entry.model_dump()}

    async def team_resident_clear(self, params: dict) -> dict:
        """team.resident_clear：解除常驻登记（未登记报错，不静默）。

        两种口径：缺省按当前会话的活动团队（与 resident_set 对偶）；显式传
        team_id 则直接按全局册解除——已交付/已收队的队伍没有活动团队可认，
        终态团队卡仍要能解除常驻（评审③：否则只能手改 team_resident.json）。
        全局册仅本机写面（dispatch 表 local_only），按 id 解除与模板按名删除
        同姿态，不做会话归属收敛。
        """
        team_id = str(params.get("team_id") or "").strip()
        if not team_id:
            _sid, orch = self._team_for(params)
            team_id = orch.team_id
        self.resident_teams.remove(team_id)
        return {"ok": True, "team_id": team_id, "resident": False}

    def team_resident_get(self, params: dict) -> dict:
        """team.resident_get：按 team_id 探询常驻登记状态（只读）。

        终态团队卡（回放/中断重建的卡没有活动快照）凭它决定是否亮出「解除
        常驻」入口；查无登记回 resident=False，不报错（探询语义）。team_id
        是全局册主轴，无会话归属面可收敛——读面也仅本机（dispatch 表
        local_only），不给远程客户端枚举本机常驻登记的入口。
        """
        team_id = str(params.get("team_id") or "").strip()
        if not team_id:
            raise TeamError("缺少 team_id：常驻登记以 team_id 为主轴")
        entry = self.resident_teams.get(team_id)
        return {"team_id": team_id, "resident": entry is not None,
                "entry": entry.model_dump() if entry is not None else None}

    # ---- 频道回放（三期）：收队/重启后按 team_id 拉全量频道消息 ----

    async def team_log(self, params: dict) -> dict:
        """team.log：按 team_id 回放某团队的全部频道消息（team_messages 表）。

        会话归属一致性校验（一致性校验，安全收敛在 WS 入口——非本机调用的
        session_id 已在 app.py 收敛到该连接绑定的活动会话，本方法按服务端侧
        认定的 sid 比对，请求原值不参与）：活动团队直接比对 sid → orch.team_id；
        团队已收队/应用重启后凭 assistant meta 里的 team.team_id 回放，按库内
        落库行的 session_id 归属判定（落库行由 _team_message_sink 戳上建队
        会话）。两证皆无（旧版本团队没落过库 / 未知 id）不拒绝——回空列表，
        前端退回截断 meta；空结果没有可泄露的内容。行字段与列序即
        store.list_team_messages。
        """
        sid = str(params["session_id"]) if params.get("session_id") is not None \
            else (self.session.id if self.session else "")
        team_id = str(params.get("team_id", "") or "").strip()
        if not team_id:
            raise TeamError("缺少 team_id：请从团队 meta 的 team.team_id 取回放主轴")
        orch = self._teams.get(sid)
        active_hit = orch is not None and bool(orch.team_id) and orch.team_id == team_id
        rows = (
            await self.store.list_team_messages(team_id) if self.store is not None else []
        )
        owned = bool(sid) and any(r.get("session_id") == sid for r in rows)
        foreign = any(r.get("session_id") and r.get("session_id") != sid for r in rows)
        if not active_hit and not owned and foreign:
            raise TeamError("team_id 不属于当前会话：不能回放其他会话的团队频道")
        return {"team_id": team_id, "session_id": sid, "messages": rows}
