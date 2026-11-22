"""对话主流程：send 入口、轮次管道、排队与取消、图片/引用上下文清洗。

D1 会话与运行时域（backend.py Mixin 拆分批 5a 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变（唯一改写：两处方法内相对
导入 `..` → `...`，因为本文件在 backend_parts/ 下一层）。

四段相邻职责：
- 入参预处理：图片附件清洗（类型/大小/数量白名单）与「& 引用对话」的
  引用清洗 / 上下文拼装；
- send：唯一对话入口——会话解析、运行级授权、引导（steer）、排队、
  团队 / 圆桌 / 对抗的同轮互斥与分流；
- _run_turn_pipeline（全库最大单方法）：模型调用、工具循环、流式合并、
  权限挂起、上下文压缩、轮末落库与收尾冲刷（轮末 / 异常 / 取消三路径
  必 flush，漏一条都会让回答缺尾——tests/test_stream_merge.py）；
- 排队执行 _run_queued、取消 cancel_run、自动标题 _auto_title、
  历史重载 _reload_agent_history 与轮次落库 _persist_turn。

内核共用件（不进本文件，由 backend.py 在自身定义完成后注入本模块命名空间，
见 backend.py 末尾的接线注记）：`StreamDeltaMerger`（流式合并内核底座，
设计 §2.0/§6 明令永不随域迁移）、`QueuedTurn`、`_stamp_turn_estimate`、
`_PERSIST_HARVEST_TIMEOUT`。backend_parts 各模块不得 import backend
（会成环，见 backend_parts/__init__.py 的文件头约定），故由内核单向注入。

跨域只读（设计 §3.3 雷区，批 6 统一收敛）：本文件内 4 处 `self._teams`
成员判定（send 一处、_run_turn_pipeline 三处，含「建队轮已开跑、_teams
尚未登记」窗口期重验守卫）——批 5a 逐字搬移、保持裸访问不动，不许顺手
改成 accessor。

字段所有权（D1，本批只注记不搬）：`_titling`（自动标题单飞集合）、
`_persist_tasks`（在飞落库任务，_persist_turn 写、delete_session 等）、
`_manually_named`（用户手动改名后不再自动起标题）由主类 __init__ 一次
铺齐（本 Mixin 无 __init__、无类级可变属性；IMAGE_* / REF_* 是不可变常量）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from ...bgtasks import spawn_bg
from ...channels import CHANNEL_STYLE
from ...core.context import estimate_tokens, is_compaction_summary
from ...core.estimate import estimate_task
from ...core.prompt import PLAN_MODE_PREFIX
from ...events import NoticeEvent, PermissionResolved, QueueUpdated, TaskEstimate
from ...messages import ImageBlock, Message
from ...models import Provider
from ...obs import info as obs_info
from ...obs import warning as obs_warning
from ...session.store import export_messages_text
from ...tools import ToolRegistry
from ...tools.memory_embed import schedule_warmup
from ...tools.shell import pop_bg_notes
from ._shared import EmitFn, SessionRuntime, collect_stream_text

if TYPE_CHECKING:
    # 循环引用的类型侧声明：backend_parts 不得运行期 import backend。这 4 个
    # 名字的定义在 backend.py（内核单一事实来源），运行期由 backend 在模块末尾
    # 单向注入本模块命名空间（见 backend.py「内核共用件接线」注记）——此处只
    # 让静态检查看到绑定，方法体因此保持逐字搬移。
    from ..backend import (
        _PERSIST_HARVEST_TIMEOUT,
        QueuedTurn,
        StreamDeltaMerger,
        _stamp_turn_estimate,
    )


class ChatPipelineMixin:
    """对话主流程（chat.send / chat.cancel / 检查点之外的全部轮次实现面）。

    方法自 backend.py 逐字搬入；状态全部来自主类共享 self（无 __init__）。
    """

    # ---- 对话主流程 ----

    # 图片附件限制：类型白名单、单条最多 4 张、单张 base64 ≤ 6M 字符（约 4.5MB 原图）
    IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
    MAX_IMAGES_PER_TURN = 4
    MAX_IMAGE_B64_CHARS = 6_000_000

    def _sanitize_images(self, images) -> list[ImageBlock]:
        """清洗前端传来的图片附件：类型/大小/数量白名单，不合规静默丢弃。"""
        out: list[ImageBlock] = []
        for im in (images or [])[: self.MAX_IMAGES_PER_TURN]:
            if not isinstance(im, dict):
                continue
            mt = str(im.get("media_type", ""))
            data = str(im.get("data", ""))
            if mt not in self.IMAGE_MEDIA_TYPES or not data or len(data) > self.MAX_IMAGE_B64_CHARS:
                continue
            out.append(ImageBlock(media_type=mt, data=data))
        return out

    # ---- 「& 引用对话」：会话引用的清洗与上下文拼装 ----

    REF_MAX_SESSIONS = 3      # 单轮最多引用的对话数
    REF_MAX_CHARS = 4_000     # 每个被引用对话注入的最大字符数（保尾留新，最近的对话最相关）

    def _sanitize_refs(self, refs: list[str] | None, exclude: str | None) -> list[str]:
        """清洗引用清单：去重、剔除目标会话自己、截断数量上限。"""
        out: list[str] = []
        for r in refs or []:
            rid = str(r)
            if not rid or rid == exclude or rid in out:
                continue
            out.append(rid)
            if len(out) >= self.REF_MAX_SESSIONS:
                break
        return out

    async def _recent_turn_seconds(self, limit: int = 30) -> list[float]:
        """本项目近期实测轮耗时（秒），任务耗时预估的历史校准样本。

        store 不可用（纯测试环境等）时返回空列表，预估退化为纯启发式。
        """
        if self.store is None or self.project is None:
            return []
        try:
            return await self.store.recent_turn_seconds(self.project.id, limit=limit)
        except Exception:  # noqa: BLE001 - 校准样本拿不到不该挡住发消息
            return []

    async def _build_refs_context(
        self, refs: list[str], project_id: int | None = None, local: bool = True,
    ) -> str:
        """把被引用会话的记录拼成注入本轮的上下文块。

        会话不存在、没有可读消息时静默跳过；超长的保留尾部（最近的对话与当前
        任务最相关），并标注省略。格式与 /export 的导出文本一致。

        项目归属（安全审查 B2）：引用列表由客户端提交，必须校验归属，否则远程
        客户端拿到别的项目的 session_id 就能把他人历史注入自己项目的上下文。
        本机桌面端（local=True）已放开跨项目引用：注入时标注来源项目，提醒
        其中提到的路径属于另一工作目录；远程客户端（local=False）仍按「本轮
        会话所属项目」严格剔除。目标项目取 runtime 的项目上下文：跨项目分屏
        列引用它自己项目的对话照常可用。快聊会话（project_id 为 NULL）在
        本机两端都可被引用，来源标注为「快聊」。
        """
        target_pid = project_id if project_id is not None else self._cur_project_id()
        blocks: list[str] = []
        for rid in refs:
            if local:
                sess = await self.store.get_session(rid)
            else:
                sess = await self.store.get_session_for_project(rid, target_pid)
            if sess is None:
                continue  # 不存在（远程端含：不属于目标项目的会话）：当作不存在，不注入
            title = await self.store.get_session_title(rid)
            try:
                msgs = await self.store.load_messages(rid)
            except Exception:  # noqa: BLE001 - 引用的会话读不到就不注入，别挡住正常提问
                continue
            text = export_messages_text(msgs).strip()
            if not text:
                continue
            if len(text) > self.REF_MAX_CHARS:
                text = "（前文过长，此处省略——以下是对话的最近部分）\n" + text[-self.REF_MAX_CHARS:]
            head = f"──── 引用对话「{title or '未命名会话'}」"
            if local and sess.project_id != target_pid:
                src = "快聊"
                if sess.project_id is not None:
                    proj = await self.store.get_project(sess.project_id)
                    src = f"项目「{proj.name}」" if proj else "其他项目"
                head += (
                    f"（来自{src}；其中提到的文件路径与约定属于另一工作目录，"
                    "仅供背景参考，不要据此操作当前项目之外的文件）"
                )
            blocks.append(head + f" ────\n{text}")
        if not blocks:
            return ""
        return (
            "[引用对话] 用户在本轮点名引用了下面的历史对话记录，回答时可参考其中内容：\n\n"
            + "\n\n".join(blocks)
            + "\n\n[引用对话结束]\n\n"
        )

    async def send(self, text: str, emit: EmitFn, plan_mode: bool = False,
                   roundtable: bool = False, members: list | None = None,
                   images: list[dict] | None = None,
                   session_id: str | None = None,
                   wants_title: bool = False,
                   regenerate: bool = False,
                   # 运行中「引导」：把这条消息注入当前轮的下一次模型调用，
                   # 而不是排队等本轮结束。没有活动轮时自动回落到普通发送。
                   steer: bool = False,
                   compare: bool = False,
                   refs: list[str] | None = None,
                   debate_rounds: int | None = None,
                   chair_answers: bool | None = None,
                   team: bool = False,
                   adversarial: bool = False,
                   director_mode: str = "user",
                   director: dict | None = None,
                   team_template: str = "",
                   template: str | dict | None = None,
                   grant: dict | None = None,
                   local: bool = True) -> dict:
        """跑一轮对话；过程事件通过 emit 推送；结束后持久化新消息。

        local=True（本机，默认）跨项目会话放行——分屏列是完整会话，runtime
        绑定会话所属项目；远程客户端（False）仍按当前项目归属校验（B 族）。

        Agent 正在工作时再次 send 不再报错，而是**排队**：等当前轮结束后
        自动依次执行（对标 Claude Code 的消息队列），fut 在该轮真正跑完时
        才返回结果。

        plan_mode=True 时本轮切换为只读工具集并附加规划指令（对标 Claude Code
        计划模式）：Agent 只做只读调研并输出实施计划，不改动任何文件。

        roundtable=True 时本轮走「圆桌」流程：多个模型并行独立作答，
        主席（当前主模型）融合成最终答案；members 为显式成员列表
        [{provider, model}]，缺省时自动选取（见 _resolve_members）。
        debate_rounds / chair_answers 为本轮覆盖值（None=用 config.toml 的
        [roundtable] 配置）：前者是额外辩论修订轮数（0-2），后者控制主席
        是否也出一份草稿。

        images 为用户消息携带的图片附件 [{media_type, data(base64)}]，
        只在普通轮生效（圆桌轮是纯文本协作，忽略图片）。

        team=True 时本轮走「团队」流程：该会话没有活动团队则先建队
        （成员来自 members 参数，缺省自动解析，上限 cfg.team.max_members），
        随后消息作为总管指令进团队频道、被 @ 点名的成员逐个唤醒。团队进行中的
        会话，后续消息自动路由进团队（即便没带 team 标志）；team 与 roundtable
        同轮互斥。director_mode="ai"（二期）建队时指定 AI 总管：director 为
        担任总管的 {provider, model}，用户消息经 run_auto_turn 驱动自动闭环
        （拆解 → 派工 → 验收 → 交付）；缺省 "user" 保持一期用户总管行为。

        adversarial=True 时本轮走「对抗」流程：四角色流水线审查——发现者穷举
        问题 → 调查者逐条对抗验证 → 建议者给修复方案 → 裁判（当前主模型）
        终审报告。members 按序承担发现/调查/建议三个角色（不足时复用同一个）；
        与 team/roundtable 三者同轮互斥。

        refs 为「& 引用对话」选中的会话 id 列表：每轮最多 REF_MAX_SESSIONS
        个，会话记录会被注入本轮上下文（见 _build_refs_context）。

        session_id 指定目标会话（多会话并行时前端按标签传入）；缺省用活动会话。
        目标会话不是当前活动会话时先就位它的 runtime（校验 + 补历史，见
        _ready_runtime_for_send）——不切换活动指针：定向发送（分屏列）不能
        劫持「当前会话」，伴生面板的取数口径不被带走。
        """
        # 协作模板（本期）：template 优先于三个布尔与 members/director——
        # 模板自带 kind，选中模板后前端不再发协作布尔；模板与布尔同给不报错，
        # 一律按模板路由。模板在入口就解析成 dict 并随本轮固定，交棒轮不回查
        # 模板册（排队期间模板可能被删除/改名）。
        tpl: dict | None = None
        if isinstance(template, dict):
            tpl = self._normalize_template_dict(template)
        elif str(template or "").strip():
            name = str(template).strip()
            tpl = self._find_collab_template(name)
            if tpl is None:
                raise RuntimeError(f"找不到协作模板：{name}")
        if tpl is not None:
            kind = str(tpl.get("kind") or "")
            team = kind == "team"
            roundtable = kind == "roundtable"
            adversarial = kind == "adversarial"
            if kind == "team" and not team_template:
                # 建队来源模板名：常驻登记据此把队伍绑回模板
                team_template = str(tpl.get("name") or "")
        # 团队/圆桌/对抗同轮互斥（设计 §13.4；对抗同规则）：前端开关联动互斥，
        # 后端多参数同给报参数错。模板轮已由模板决定 kind，跳过这条检查。
        if tpl is None and sum(bool(m) for m in (team, roundtable, adversarial)) > 1:
            raise RuntimeError(
                "「团队」「圆桌」「对抗」不能同时开启，同一条消息只能选一个。"
            )
        # 轮次起点重验工作区信任（审查 P2-4）：会话运行期间项目级配置被外部
        # 改动（git pull 等）时，趁本轮开始断开项目级 MCP、重发现技能。
        await self.recheck_trust_before_turn()
        # 钉住目标会话（并发正确性）：send 执行期间有多个 await（激活、用量、
        # 引用清洗…），期间并发请求可以改写全局活动指针 self.session（另一条
        # send / session.activate 都会）。之后一律用入口钉住的 target_sid 推导
        # runtime 与落库，不再从 self.session 二次读取——否则 A 会话的轮会跑到
        # B 会话的 runtime 里并落库到 B。
        target_sid = str(session_id) if session_id else (
            self.session.id if self.session else None
        )
        if session_id and (not self.session or self.session.id != session_id):
            # 目标会话就位（归属校验 + 建 runtime + 补历史）但不动活动指针：
            # 定向发送不劫持「当前会话」，主栏伴生面板（审查/任务清单/上下文
            # 浮层）读到的仍是主栏会话，ui.json 的活动会话也不被覆写
            await self._ready_runtime_for_send(session_id, local=local)
        # 会话级活动团队：进行中的团队把该会话的后续消息全部吸进团队频道
        #（设计 §3——团队进行中不再进普通回合），即便前端没带 team 标志。
        active_team = target_sid is not None and target_sid in self._teams
        if active_team and (roundtable or adversarial):
            mode_name = "圆桌" if roundtable else "对抗"
            raise RuntimeError(
                f"当前会话有进行中的团队，消息会进入团队频道；请先收队再使用{mode_name}。"
            )
        team_bound = team or active_team
        # 运行级授权（grant）：团队轮消费（模板团队或 team=true 直发都算）——
        # 圆桌/对抗本期是纯文本协作、不挂工具，传了也忽略。此前只认模板路径，
        # 直发 team=true + grant 被静默吞掉：成员写文件走逐次确认，无人应答
        # 即 300 秒超时。远程连接一律降级为逐次确认（与「放宽权限的
        # 档位只能本机切换」同一安全口径：模板数据绝不能借远程请求关掉权限门）。
        grant_info: dict | None = None
        if (tpl is not None and tpl.get("kind") == "team") or (team and tpl is None):
            raw_grant = grant if isinstance(grant, dict) else {}
            scope = str(raw_grant.get("scope") or "ask_each").strip()
            if scope not in ("run", "ask_each"):
                scope = "ask_each"
            read_only = bool(raw_grant.get("read_only"))
            degraded = scope == "run" and not local
            if degraded:
                scope = "ask_each"
            grant_info = {"scope": scope, "read_only": read_only}
            if degraded:
                await emit(NoticeEvent(
                    message="远程连接不能使用「本次运行免确认」授权，"
                            "已降级为逐次确认。"
                ).model_dump())
            if active_team:
                # 成员 Agent 的权限门在建队时固定（成员缓存 gate）：运行级授权
                # 只随新队伍生效，现有团队沿用建队时的授权面
                src_name = str((tpl or {}).get("name", "") or "").strip()
                src_label = f"模板「{src_name}」" if src_name else "本次授权"
                await emit(NoticeEvent(
                    message=f"当前会话已有进行中的团队，{src_label}"
                            "只影响新队伍；现有团队的权限范围不变。"
                ).model_dump())
        # 引用排除目标会话自己：引用当前对话没有意义
        clean_refs = self._sanitize_refs(refs, exclude=target_sid)
        if self.provider is None:
            raise RuntimeError(
                "尚未配置可用的模型 API Key——"
                "打开 ⚙ 设置 · 模型服务，选一个服务粘贴 API Key 即可开始对话。"
            )
        # 每日 token 预算护栏（设置 · 高级，默认 0 = 不限制）：超限即停，
        # 给可读的出路而不是让费用悄悄滚大。演示模式不受限（不产生真实费用）。
        if self.cfg.daily_token_budget > 0 and not getattr(self.provider, "demo_mode", False):
            used_today = await self.store.usage_today()
            # None = 用量库读不到（2026-11 except 审计 #9 取严口径）：护栏保持
            # 原「读不到按未越线放行」——坏仪表不停对话；取严落在预算告警侧
            if used_today is not None and used_today >= self.cfg.daily_token_budget:
                raise RuntimeError(
                    f"已触发每日 token 预算护栏：今天已用约 {used_today:,} tokens，"
                    f"达到上限 {self.cfg.daily_token_budget:,}。\n"
                    "如需继续，请在 设置 · 高级 里调高或关闭「每日 token 预算」"
                    "（明天自动重置）。"
                )
        # 图片附件：本轮实际生效的服务声明"不支持图片输入"时提前给可读提示，
        # 而不是把图片塞给纯文本模型，换回一句上游报错（用户不知道是模型选错了）。
        # 按目标会话解析（三段：覆盖 → 新会话默认 → 全局）——会话级换过模型的
        # 分屏列/标签，判的是它自己的服务；拿全局记账判会双向误判（全局不支持/
        # 覆盖支持被误拒、全局支持/覆盖不支持放行给上游报错），错误文案也会报成
        # 没在用的服务名。
        clean_images = self._sanitize_images(images)
        # 团队轮是纯文本协作，图片本就不会发给队员（编排器会发 Notice 并忽略），
        # 不因主模型不支持图片拦人
        if clean_images and not team_bound:
            eff_name, eff_model = (
                self._session_effective_model(target_sid) if target_sid
                else (self.provider_name or "", self.provider_model or "")
            )
            pc_eff = self.cfg.providers.get(eff_name) if eff_name else None
            # 未配置的服务（测试注入等）按支持处理，不拦人（与 _supports_vision 同口径）
            if pc_eff is not None and not pc_eff.supports_vision:
                raise RuntimeError(
                    f"「{eff_name} / {eff_model or pc_eff.model}」不支持图片输入，图片发不出去。\n"
                    "请在输入框的模型选择器里换一个多模态模型（如 GLM-4V、Kimi、GPT-4o 等），"
                    "或在 设置 · 模型服务 里把该服务的「支持图片输入」打开。"
                )
        # 运行中「引导」：当前会话有普通单 Agent 轮在跑时，消息直接进它的
        # 收件箱（下一次模型调用前插入历史），不排队、不另起一轮。多模型
        # 协作轮（圆桌/团队/对抗）不接受主 Agent 收件箱，回落到下面的排队。
        if steer and target_sid:
            rt_steer = self.runtimes.get(target_sid)
            if (
                rt_steer is not None
                and rt_steer.steerable
                and rt_steer.run_task is not None
                and not rt_steer.run_task.done()
                and rt_steer.agent.steer(text, images=clean_images)
            ):
                # 广播给其它在线客户端（发起端已本地渲染气泡，按 emit 身份
                # 排除；与普通轮末尾的 user_message 广播同一形状）。
                source_emit = getattr(emit, "_source_emit", emit)
                if text or clean_images:
                    u_ev = {
                        "kind": "user_message", "session_id": target_sid, "text": text,
                        "images": [
                            {"media_type": b.media_type, "data": b.data}
                            for b in (clean_images or [])
                            if getattr(b, "type", "") == "image"
                        ],
                    }
                    for ws_emit in list(self.ws_emitters):
                        if ws_emit is source_emit:
                            continue
                        try:
                            spawn_bg(ws_emit(u_ev))
                        except RuntimeError:
                            break  # 无事件循环（纯测试环境）
                return {"steered": True, "session_id": target_sid}
        # 在任何 await 之前先占住运行位：否则两条背靠背到达的消息会在
        # new_session() 的挂起点上双双判为空闲、并发执行（撞消息表唯一约束）。
        # 占位判断同时看基底位（会话懒创建窗口）与活动 runtime 位；后来者排队。
        # 全程用钉住的 target_sid，不重读 self.session（见上）。
        cur = asyncio.current_task()
        rt_now = self.runtimes.get(target_sid) if target_sid else None
        holder = rt_now.run_task if rt_now else self._base_run_task
        if holder is not None and not holder.done() and holder is not cur:
            loop = asyncio.get_running_loop()
            item = QueuedTurn(
                text=text, emit=emit, plan_mode=plan_mode, fut=loop.create_future(),
                roundtable=roundtable, members=members,
                images=self._sanitize_images(images),
                refs=clean_refs, compare=compare,
                debate_rounds=debate_rounds, chair_answers=chair_answers,
                team=team, adversarial=adversarial,
                director_mode=director_mode, director=director,
                team_template=team_template,
                template=tpl, grant=grant_info,
                local=local,
            )
            if rt_now is not None:
                rt_now.queue.append(item)
                q_pending = len(rt_now.queue)
            else:
                self._base_queue.append(item)
                q_pending = len(self._base_queue)
            # 排队状态是所有连接的共同事实：只发发起连接，其他端（手机遥控、
            # 第二个窗口）的运行态会停在旧值——且发起者断开后连它自己也没了
            # 收尾广播的接收方。发起连接走 emit 保序，其余连接广播。
            await emit(QueueUpdated(pending=q_pending, session_id=target_sid or "").model_dump())
            self._broadcast_ws(
                QueueUpdated(pending=q_pending, session_id=target_sid or "").model_dump(),
                exclude=getattr(emit, "_source_emit", emit),
            )
            return await item.fut
        # 全局并行轮数上限（评审加固）：单会话排队（上面）不增加全局并发——交棒
        # 接替的是刚结束的那轮；真正新起一轮的 send 才受这道闸。定时任务/流水线
        # 用本地 runtime，不进 self.runtimes，不受也不占这额度。
        if self._running_turn_count() >= self.MAX_INTERACTIVE_TURNS:
            raise RuntimeError(
                f"当前并行任务太多（已有 {self.MAX_INTERACTIVE_TURNS} 个会话同时在跑），"
                "请等部分会话的当前轮结束，或先停止不需要的轮次再发送。"
            )
        self._base_run_task = cur  # 抢占基底位，掩护随后的懒创建 await 窗口
        if target_sid is None:
            await self.new_session()  # 懒创建：第一条消息才落库
            target_sid = self.session.id if self.session else None
        runtime = await self._get_runtime(target_sid)
        runtime.run_task = cur
        self._base_run_task = None  # 运行位已落到 runtime，基底占位清除
        try:
            return await self._run_turn_pipeline(
                text, emit, plan_mode, roundtable=roundtable, members_params=members,
                images=clean_images, runtime=runtime,
                session_id=target_sid, wants_title=wants_title,
                regenerate=regenerate, compare=compare, refs=clean_refs,
                debate_rounds=debate_rounds, chair_answers=chair_answers,
                team=team, adversarial=adversarial,
                director_mode=director_mode, director=director,
                team_template=team_template,
                template=tpl, grant=grant_info,
                local=local,
            )
        except asyncio.CancelledError:
            # 用户在 turn 真正开始前点了停止：无产出，返回诚实的 stopped 结果，
            # 而不是让任务以异常结束（run_task 已经释放，后续消息不会进死队列）。
            if runtime.run_task is asyncio.current_task():
                runtime.run_task = None
            if self._base_run_task is asyncio.current_task():
                self._base_run_task = None
            return {
                "done": True,
                "stopped": True,
                "session_id": runtime.sid,
                "plan_mode": plan_mode,
                "roundtable": False,
                "adversarial": False,
                "team": None,
                "context_tokens": runtime.agent.used_context_tokens(),
                "context_limit": runtime.agent.context_limit_tokens,
                "context_detail": self._context_detail(runtime.agent),
            }
        except BaseException as e:
            # 备用模型自动切换（可选配置 fallback_model）：主模型失败时切到
            # 备用服务，用户下一轮直接用新模型；本轮错误照常上抛。
            if isinstance(e, Exception) and not isinstance(e, asyncio.CancelledError):
                try:
                    from ...models.diagnostics import diagnose_error as _diag
                    d = _diag(e)
                    if d.category in ("auth", "balance", "server", "model_missing"):
                        await self._switch_to_fallback(reason=d.title)
                except Exception:  # noqa: BLE001 - 切换失败不打断原异常
                    pass
            # pipeline 启动阶段就抛出（还没走到它自己的标志管理）时把位让出来，
            # 否则后续所有消息都会误入永远无人消费的队列。
            if runtime.run_task is asyncio.current_task():
                runtime.run_task = None
            if self._base_run_task is asyncio.current_task():
                self._base_run_task = None
            raise

    async def _auto_title(self, sid: str, first_user: str, first_reply: str) -> None:
        """首轮结束后用当前模型生成简短标题；失败保持原状，绝不影响主流程。"""
        if getattr(self.provider, "demo_mode", False):
            return  # 演示模式不额外消耗脚本组，标题保持首行截断即可
        if sid in self._titling:
            return
        if sid in self._manually_named:
            return  # 用户手改过名字：自动标题不覆盖，尊重命名
        self._titling.add(sid)
        try:
            cur_title = await self.store.get_session_title(sid)
            seed = f"用户：{first_user[:400]}"
            if first_reply:
                seed += f"\n助手：{first_reply[:300]}"
            prompt = (
                "给下面的对话起一个不超过12个字的中文标题，概括主题。"
                "只输出标题本身，不要引号、句号或任何解释。\n\n" + seed
            )
            raw = (await collect_stream_text(self.provider, prompt)).strip()
            title = raw.splitlines()[0].strip(' \t"“”「」』【】。，；：')[:24] if raw else ""
            if not title or title == cur_title:
                return
            await self.store.set_title(sid, title)
            for ws_emit in list(self.ws_emitters):
                try:
                    await ws_emit({"kind": "session_updated",
                                   "session_id": sid, "title": title})
                except Exception:  # noqa: BLE001
                    pass
            # 主屏首轮自动标题落地后，它在飞的接力副屏标题跟着对齐
            # （旧标题 = 自动标题前库里那份，供孤儿副屏按旧名追）
            await self.relay_sync_peer_title(sid, title, cur_title)
        except Exception:  # noqa: BLE001
            pass  # 标题生成失败完全静默
        finally:
            self._titling.discard(sid)

    async def _reload_agent_history(
        self, agent, sid: str, system: str | None = None
    ) -> list[Message]:
        """从存储重载会话历史，并保证系统提示词仍在最前面；返回重载到的消息。

        system 消息从不落库——它由 compose_system() 按当前技能/项目/工具实时生成，
        落库等于把过期提示词固化下来。所以 load_history(msgs) 之后必须补回：
        少了这一步，重载过的会话下一轮会带着「没有系统提示词」的历史去调模型，
        Agent 的工具用法约定、项目上下文、技能清单全部丢失。旧写法
        `load_history(msgs or [Message.system(...)])` 只兜住了空会话，非空会话
        （也就是真正需要重载的那些）正好漏掉。
        """
        msgs = await self.store.load_messages(sid)
        agent.load_history(msgs)
        if system is None:
            # 跨项目分屏列的 runtime 绑定会话所属项目：系统提示词按它自己的
            # 工作目录组装（cwd 与项目约定对齐）；其余走 compose_system()
            # 既有接缝（输出等价）。
            rt = self.runtimes.get(sid)
            if rt is not None and rt.ctx is not None and rt.ctx.own:
                system = self.compose_system_for(rt.ctx.workdir)
            else:
                system = self.compose_system()
            # 渠道会话：把 IM 的输出纪律写进系统提示词（结论先给、100 字内、
            # 不贴日志/代码）。下在生成侧而不是发出去前截断——截断会把结论
            # 切掉，用户看到半句话还以为 Agent 没做完。
            if sid in self._channel_names:
                system += CHANNEL_STYLE
        agent.set_system(system)
        return msgs

    async def _persist_turn(
        self, sid: str, new_msgs: list, summary_src: list | None = None,
    ) -> None:
        """一轮对话的落库（独立方法便于 shield 保护：取消时后台完成落库）。

        new_msgs 只含「还没在途落库」的消息；摘要要从本轮全部新增里挑，
        否则增量落库已经把最终回答写掉的轮次会算不出会话摘要。
        """
        for m in new_msgs:
            await self.store.append_message(sid, m)
        await self.store.touch(sid)
        summary = next(
            (m.text.strip().replace("\n", " ")[:120]
             for m in reversed(summary_src if summary_src is not None else new_msgs)
             if m.role == "assistant" and m.text.strip()),
            "",
        )
        if summary:
            await self.store.set_summary(sid, summary)

    async def _run_turn_pipeline(
        self, text: str, emit: EmitFn, plan_mode: bool,
        roundtable: bool = False, members_params: list | None = None,
        images: list[ImageBlock] | None = None,
        runtime: SessionRuntime | None = None,
        session_id: str | None = None,
        wants_title: bool = False,
        regenerate: bool = False,
        compare: bool = False,
        refs: list[str] | None = None,
        debate_rounds: int | None = None,
        chair_answers: bool | None = None,
        team: bool = False,
        adversarial: bool = False,
        director_mode: str = "user",
        director: dict | None = None,
        team_template: str = "",
        template: dict | None = None,
        grant: dict | None = None,
        local: bool = True,
    ) -> dict:
        """真正执行一轮对话（含持久化、规划模式切换、检查点保存）。

        多会话并行：全程使用 runtime 内的 agent/queue/recorder，
        不碰 self.agent 等活动会话指针；事件发出时注入 session_id 供前端路由。
        local 标记客户端端别（本机桌面/远程）：决定引用注入的项目归属口径
        （见 _build_refs_context）。
        """
        if runtime is None:
            if self.session is None:
                await self.new_session()  # 懒创建：第一条消息才落库
            runtime = await self._get_runtime(self.session.id)
        sid = session_id or runtime.sid
        agent = runtime.agent
        # 本轮是否接受「引导」注入：普通单 Agent 轮可以；圆桌/团队/对抗
        # 是多模型流水线，主 Agent 收件箱不参与那些阶段，发送方回落到排队。
        runtime.steerable = not (
            roundtable or team or adversarial or (sid in self._teams)
        )
        # 单轮 token 上限（单轮护栏）：估算本轮请求体积（历史 + 本条消息），
        # 超上限直接拒绝——每日预算防线的前置，避免「一轮就烧掉全天额度」。
        # 0 = 不限制；演示模式同样豁免。估算偏保守（消息文本按 1/3 折算）。
        if self.cfg.turn_token_budget > 0 and not getattr(self.provider, "demo_mode", False):
            est_turn = estimate_tokens(list(agent.history)) + len(text or "") // 3
            if est_turn > self.cfg.turn_token_budget:
                raise RuntimeError(
                    f"本轮请求约 {est_turn:,} tokens，超过单轮上限 "
                    f"{self.cfg.turn_token_budget:,}。\n"
                    "可在输入框执行 /compact 压缩历史，或在 设置 · 高级 里"
                    "调高「单轮 token 上限」（0 = 不限制）。"
                )

        # 流式增量合并：emit_ev 下发给前端前先过一层缓冲，把连续的同类型增量
        # （text_delta / thinking_delta / roundtable_member_delta）拼成批量帧。
        # 其余事件在 merger 里会先冲刷缓冲再原样发出，顺序与合并前一致。
        async def emit_logged(payload: dict) -> None:
            # 先记进回放缓冲再下发：连接断掉时 emit 静默失败，但帧仍留在
            # 缓冲里供重连补发（seq 记的是「发生过」而不是「送达过」）。
            await emit(self.log_session_event(sid, payload))

        merger = StreamDeltaMerger(emit_logged)

        # 本轮工具/权限的耗时统计（给轮末结构化日志用）。
        # 从事件流里数而不是侵入 agent 循环：事件本就带 duration_ms，
        # 这里只做累加，不在热路径上加任何计算。
        # slowest_tool(_ms) 记本轮最慢的一次工具调用——「这轮为什么慢」最常
        # 是某一个慢工具，只有一个总数看不出是谁。
        turn_stats: dict[str, int] = {
            "tool_calls": 0, "tool_ms": 0, "tool_errors": 0,
            "permission_waits": 0, "permission_ms": 0,
            "slowest_tool": "", "slowest_tool_ms": 0,
            "dup_tool_calls": 0,
        }
        _perm_pending: dict[str, float] = {}
        # 重复调用计数（无效步骤信号）：本轮「同工具 + 同参数」的再次尝试。
        # 按 tool_call_started 数（被 loop_guard 拦下的重试也发 started）——
        # 信用分配只到轮级：这个数 tells「这轮有多少步是白走的」。
        _call_seen: dict[tuple[str, str], int] = {}
        # 本轮失败证据：Agent 会把 Provider 异常吞成 ErrorEvent 后正常收尾，
        # 外层看不到 Python 异常。只靠 turn_exc 判定终态会把失败轮写成
        # completed（运行中心显示“已完成”但错误摘要还在）。这里显式收集
        # 致命失败信号：ErrorEvent 与 TurnFinished(stop_reason="error")。
        turn_fail: dict[str, str] = {}

        async def emit_ev(ev: dict) -> None:
            kind = ev.get("kind")
            # 运行中心状态跟踪（best-effort：登记不存在时 update 内部 no-op）
            if kind == "tool_call_started":
                self.runs.update(run_id, state="tool_running",
                                 detail="正在执行 " + str(ev.get("name") or ""))
                # 同工具 + 同参数的再次尝试：重复劳动计数（信用分配的轮级代理）
                _ck = (
                    str(ev.get("name") or ""),
                    json.dumps(ev.get("input") or {}, ensure_ascii=False,
                               sort_keys=True, default=str)[:2000],
                )
                _seen = _call_seen.get(_ck, 0) + 1
                _call_seen[_ck] = _seen
                if _seen > 1:
                    turn_stats["dup_tool_calls"] += 1
            elif kind == "tool_call_finished":
                self.runs.update(run_id, state="thinking")
            elif kind == "permission_request":
                self.runs.update(run_id, state="waiting_permission",
                                 detail="等待确认：" + str(ev.get("tool_name") or ""))
                # 权限请求推渠道（P21 剩余半边，默认关）：只提醒不审批，
                # fire-and-forget，不拖慢确认卡出现。
                self._fire_permission_push({**ev, "session_id": sid})
            elif kind == "permission_resolved":
                self.runs.update(run_id, state="thinking")
            elif kind == "notice" and "自动重试" in str(ev.get("message") or ""):
                self.runs.update(run_id, state="retrying",
                                 detail=str(ev.get("message") or "")[:200])
            elif kind == "error":
                # 本轮出现引擎级错误：记下来供终态收口判定（不在这里直接定终态，
                # 因为团队/圆桌降级等路径可能随后仍有可用产出）。category 是
                # diagnostics 的分类（auth/rate_limit/context_overflow…），
                # 事后归因比一句英文报错有用。
                turn_fail.setdefault("error", str(ev.get("message") or "")[:300])
                turn_fail.setdefault("category", str(ev.get("category") or ""))
                self.runs.update(
                    run_id, state="failed", error=str(ev.get("message") or "")[:300],
                    detail="")
            elif kind == "turn_finished":
                if str(ev.get("stop_reason") or "") == "error":
                    turn_fail.setdefault("stop_reason", "error")
            if kind == "tool_call_finished":
                _ms = int(ev.get("duration_ms") or 0)
                turn_stats["tool_calls"] += 1
                turn_stats["tool_ms"] += _ms
                # 首次调用先占位（0ms 的瞬时工具也要有名字），之后严格更慢的才替换
                if turn_stats["slowest_tool"] == "" or _ms > turn_stats["slowest_tool_ms"]:
                    turn_stats["slowest_tool_ms"] = _ms
                    turn_stats["slowest_tool"] = str(ev.get("name") or "")
                if ev.get("is_error"):
                    turn_stats["tool_errors"] += 1
            elif kind == "permission_request":
                rid = ev.get("request_id") or ""
                if rid:
                    _perm_pending[rid] = time.monotonic()
            elif kind == "permission_resolved":
                rid = ev.get("request_id") or ""
                t0 = _perm_pending.pop(rid, None)
                if t0 is not None:
                    turn_stats["permission_waits"] += 1
                    turn_stats["permission_ms"] += int((time.monotonic() - t0) * 1000)
            # 消息边界增量落库（见 _persist_progress）：只在历史真正变化的事件上
            # 写库，文本增量不碰 DB。用户消息与助手消息是两个用户最在意的边界，
            # 强制写、不吃 1 秒节流——崩溃丢掉刚发的一句 / 刚答完的一段最扎眼；
            # 工具结束与权限请求密集，仍按节流写，避免把写锁竞争放大
            if kind in (
                "turn_started", "assistant_message", "tool_call_finished",
                "turn_finished", "permission_request",
            ):
                await _persist_progress(
                    force=kind in ("turn_started", "assistant_message")
                )
            await merger.send({**ev, "session_id": sid})

        # 「用户消息」广播给其余在线客户端：轮次事件只发给发起连接，其他窗口 /
        # 手机端此前既看不到用户消息也没有任何「有人发了话」的来源。发起连接的
        # 前端已在 send() 里本地渲染过气泡，按对象身份排除，避免重复渲染。
        # 重新生成（text 为空串）不广播。
        # chat.send 在 WS 层用 request_emit 包一层以便懒绑定远程会话；包装器
        # 仍代表同一条连接。优先取它挂载的稳定源 emitter，否则普通内部调用
        # 仍按传入的 emit 身份排除发送方。
        source_emit = getattr(emit, "_source_emit", emit)
        others = [w for w in list(self.ws_emitters) if w is not source_emit]
        if others and (text or images):
            u_ev = {
                "kind": "user_message", "session_id": sid, "text": text,
                "images": [
                    {"media_type": b.media_type, "data": b.data}
                    for b in (images or []) if getattr(b, "type", "") == "image"
                ],
            }
            for ws_emit in others:
                try:
                    spawn_bg(ws_emit(u_ev))
                except RuntimeError:
                    continue  # 无事件循环（如纯测试环境）

        sess_title = await self.store.get_session_title(sid)
        if not sess_title and not regenerate:
            sess_title = text[:40] or ("[图片]" if images else "")
            await self.store.set_title(sid, sess_title)

        # 任务耗时预估：接手任务时按启发式 + 本项目近期实测给出预计区间，
        # 先于本轮任何输出事件到达，前端显示「预计 X~Y 分钟」并对照已用时。
        # 重新生成轮（text 为空）不算接手新任务，不发。
        # turn_estimate 随轮末消息一起落库：刷新/重进会话后「用时 X · 预估 Y」
        # 芯片仍能重建（旧消息没有该字段时前端不渲染）。
        turn_estimate: dict | None = None
        if text or images:
            est = estimate_task(
                text,
                history=agent.history,
                images=len(images or []),
                members=len(members_params or []) if (roundtable or adversarial) else 0,
                debate_rounds=(debate_rounds or 0) if roundtable else 0,
                recent=await self._recent_turn_seconds(),
            )
            turn_estimate = {
                "min_seconds": est.min_seconds,
                "max_seconds": est.max_seconds,
                "level": est.level,
                "basis": est.basis,
            }
            await emit_ev(TaskEstimate(
                min_seconds=est.min_seconds,
                max_seconds=est.max_seconds,
                level=est.level,
                basis=est.basis,
            ).model_dump())

        readonly_registry = None
        if plan_mode:
            from ...tools.base import Safety

            readonly_registry = ToolRegistry(
                [t for t in agent.registry.all() if t.safety == Safety.READONLY]
            )
            agent.registry = readonly_registry
        # 记忆检索化（第一期）：轮首用「本轮用户消息 + 最近对话摘要」作查询重组
        # 系统提示词——超过 MEMORY_RETRIEVAL_THRESHOLD 的记忆只注入相关条目
        # （tools/memory.py 的 render_memory_section）；不超过阈值（或全部 0 分）
        # 时组装结果与原先逐字节一致，这里的 set_system 只是刷新同文本的
        # system 消息（system 从不落库，本就每处实时生成，见 _reload_agent_history）。
        memory_query = self._memory_retrieval_query(text, agent)
        if memory_query:
            # 嵌入是增强，绝不能阻塞事件循环（memory_embed 的性能不变量）：
            # 检索分支可能同步探测 / 嵌入 Ollama 向量，整体丢 worker 线程组装
            # （墙钟预算在 memory_embed 内部约束，慢 Ollama 也只拖慢本轮几秒），
            # 同时后台预热词条向量——暖齐后轮首只剩查询向量一次请求
            schedule_warmup()
            if runtime.ctx is not None and runtime.ctx.own:
                # 跨项目分屏列的会话按它自己的工作目录组装
                agent.set_system(await asyncio.to_thread(
                    self.compose_system_for, runtime.ctx.workdir,
                    memory_query=memory_query,
                ))
            else:
                agent.set_system(await asyncio.to_thread(
                    self.compose_system, memory_query=memory_query,
                ))
        # 团队轮例外含自动路由轮：引擎前缀一律不拼——频道消息保持用户原文
        #（§13.5 一律进频道、不做意图识别）——按 team_bound 判，不按本轮
        # flag 判（活动团队把没带标志的消息也吸进频道，前缀漏进频道文本会
        # 误导队员）。团队期间攒下的注记等收队/交付后的下一普通轮照常注入。
        team_bound = team or sid in self._teams
        # 「& 引用对话」：把被引用会话的记录拼在消息最前面注入本轮上下文
        #（与 PLAN_MODE_PREFIX 同一套做法，随用户消息一起持久化）。
        if refs and not team_bound:
            refs_ctx = await self._build_refs_context(
                refs, runtime.ctx.project_id if runtime.ctx is not None else None,
                local=local,
            )
            if refs_ctx:
                text = refs_ctx + text
        if plan_mode and not team_bound:
            text = PLAN_MODE_PREFIX + text
        # 自上一轮以来结束、还没人取报告的后台子代理任务 → 注入一条系统提示，
        # 主 Agent 开轮就知道「有任务做完了」，不用用户来催（后台模式闭环）。
        # 团队轮不注入：那是主会话轮次的词汇，暂缓到团队结束后的下一普通轮
        # （注记留在簿子里不取走，收队/交付后 _teams 移出，自然轮到它）。
        if self.tasks and not team_bound:
            bg_note = self.tasks.pop_turn_note(sid)
            if bg_note:
                text = bg_note + text
        # 后台进程（run_command background=true）退出提示：同一套「上一轮之后
        # 结束了什么」的注入口径。以前只有子代理有这条通道，命令类后台进程
        # 得靠模型自己反复 action="read" 轮询，一次长安装就吃掉几轮上下文。
        # 团队轮不取（与子代理注记同一条守卫）：注记留在簿子里，收队/交付后
        # 的下一普通轮自然轮到；否则会被拼进总管指令原样落频道。
        if not team_bound:
            shell_note = pop_bg_notes(sid)
            if shell_note:
                text = shell_note + text

        # 轮末落库靠「消息身份」挑出本轮新增，不靠下标切片：compact_history 会在
        # 本轮内把 history 整体换成更短的新列表（core/context.py），旧下标随即失效：
        # 新长度 ≤ n_before 时 `history[n_before:]` 为空（本轮用户消息、工具调用、
        # 回答全部不落库，重启即丢），略大时又会把早已入库的旧消息重复插入。
        # 消息 id 由 Message 默认工厂生成、随序列化保留，压缩后 recent 段仍是原对象
        # （id 不变），所以「不在轮前 id 集合里」正好等于「本轮新增」。
        pre_ids = {m.id for m in agent.history}
        # 在途轮增量落库：旧口径是轮末一次性批量写，进程崩在轮中（长工具、长
        # 思考、用户强退）时这一轮的用户消息、已完成的助手消息与工具结果全部
        # 丢失，重启后像是「没发过」。这里在消息边界上把本轮新增补进库：
        # 崩在两次边界之间最多丢当前一小段，缺的 tool_result 由下一轮的
        # repair_dangling_tool_uses 补成 interrupted 结果（协议仍自洽）。
        # 轮末仍走 _persist_turn，只写还没落过的那些（按消息 id 去重）。
        persisted_ids: set[str] = set()
        # 进度写节流：消息边界事件很密（每次工具结束都来一次），每条都写会把
        # 写锁竞争放大；1 秒一次足够抗崩溃，轮末还有一次全量兜底。
        _progress_at = [0.0]
        _PROGRESS_MIN_INTERVAL = 1.0

        async def _persist_progress(force: bool = False) -> None:
            # best-effort：在途落库只是抗崩溃的保险，失败不能打断正在跑的轮次
            # （轮末 _persist_turn 还会兜一次；真写不进去时那里也会报错）
            now = time.monotonic()
            if not force and now - _progress_at[0] < _PROGRESS_MIN_INTERVAL:
                return
            async with self._progress_write_lock:
                for m in list(agent.history):
                    if m.id in pre_ids or m.id in persisted_ids:
                        continue
                    if is_compaction_summary(m):
                        continue
                    try:
                        await self.store.append_message(sid, m)
                    except Exception as e:  # noqa: BLE001
                        logging.getLogger("skysheep").warning(
                            "在途消息落库失败（下一处边界或轮末重试）：%s", e
                        )
                        return
                    persisted_ids.add(m.id)
            _progress_at[0] = time.monotonic()

        # 本轮墙钟起点：圆桌路径没有 agent 主循环计时，这里兜底（见 _stamp_turn_estimate）
        turn_t0 = time.monotonic()
        stopped = False
        rt_meta: dict | None = None
        tm_meta: dict | None = None
        adv_meta: dict | None = None
        tin0, tout0 = agent.total_in_tokens, agent.total_out_tokens
        tcached0 = agent.total_cached_tokens
        runtime.run_task = asyncio.current_task()
        # 运行中心登记：这轮作为一条可观测运行进全局列表（状态随事件更新，
        # 终态在 finally 收口）。id 由 RunCenter 分配，带时间戳，跨重启不重复
        # （旧实现用进程内自增 turn-<n>，重启后从 turn-1 重来，会覆盖旧记录）。
        run_id = self.runs.allocate_id("turn")
        run_retry_text = "" if regenerate else text
        self.runs.register(
            run_id, "turn", sid,
            title=(sess_title or (text[:40] if text else "[图片]")),
            state="thinking" if not roundtable else "thinking",
            retry_text=run_retry_text,
        )
        turn_exc: BaseException | None = None
        # A3 模型自动分层：普通单 Agent 轮可能临时换 provider，轮末必须还原
        routed_prev: Provider | None = None
        routed_now: Provider | None = None
        try:
            if team or sid in self._teams:
                if roundtable or adversarial:
                    # 排队轮重验团队/圆桌/对抗同轮互斥（send() 的守卫只在入队
                    # 时刻判定）：消息在「建队轮已开跑、_teams 尚未登记」的窗口
                    # 入队时 roundtable/adversarial=true 被接受，交棒到这里团队
                    # 已在板——不重验的话协作标志会被本分支静默吞掉（0 个事件、
                    # 无任何报错）。与不入队即被拒的口径对齐，显式报错交还调用方。
                    raise RuntimeError(
                        "当前会话有进行中的团队，消息会进入团队频道；请先收队再使用协作模式。"
                    )
                # 团队轮：进行中的团队把消息吸进频道（即便 flag 未带），team=true
                # 且无活动团队则先建队。取消在编排器内收敛成「半截发言定稿进频道」
                # 后原样上抛，由外层 except 收口为 stopped。
                tm_meta = await self._team_body(
                    text, emit_ev, members_params, agent=agent, sid=sid,
                    images=images, allow_create=team, recorder=runtime.recorder,
                    plan_mode=plan_mode,
                    director_mode=director_mode, director=director,
                    template_name=team_template,
                    template=template, grant=grant,
                    local=local,
                )
            elif roundtable:
                rt_meta = await self._roundtable_body(
                    text, emit_ev, members_params, agent=agent, compare=compare,
                    sid=sid, images=images,
                    debate_rounds=debate_rounds, chair_answers=chair_answers,
                    regenerate=regenerate, template=template,
                )
                # 圆桌的取消在引擎层收敛（保留已产出的部分文本），这里同步停止位
                if (rt_meta or {}).get("status") == "cancelled":
                    stopped = True
            elif adversarial:
                adv_meta = await self._adversarial_body(
                    text, emit_ev, members_params, agent=agent, sid=sid,
                    images=images, regenerate=regenerate, template=template,
                )
                # 对抗的取消同样在引擎层收敛（已裁决结论与部分报告文本保留）
                if (adv_meta or {}).get("status") == "cancelled":
                    stopped = True
            else:
                routed_prev, routed_now = await self._apply_model_routing(
                    agent, sid, text, regenerate=regenerate, emit=emit_ev,
                )
                async for ev in agent.run_turn(text, images=images, append_user=not regenerate):
                    await emit_ev(ev.model_dump())
                    if ev.kind == "permission_request":
                        await self.store.touch(sid)
        except asyncio.CancelledError:
            stopped = True
        except Exception as e:  # noqa: BLE001
            # 意外错误也要走完整收尾（落库/用量/队列交棒/释放运行位），
            # 否则 runtime.run_task 悬挂、后续消息永远排队。收尾后原样上抛，
            # 让 WS 层返回 ok=false 的诚实错误。
            turn_exc = e
        finally:
            # 引导收件箱随本轮关闭：此后到达的 steer 一律回落到排队
            runtime.steerable = False
            # 先恢复完整工具集（只读注册表只在本轮生效）——同步操作必须放在
            # finally 里第一个 await 之前：第二次取消（用户连点两次停止，或
            # 停止与轮末竞态）只能在 await 点投递，同步段不会被跳过。若恢复
            # 被跳过，runtime 常驻，该会话之后所有轮次都拿着只读注册表跑。
            if plan_mode and readonly_registry is not None:
                agent.registry = self._build_full_registry(runtime.recorder, ctx=runtime.ctx)
            # A3：还原路由换上的临时 provider 并释放（被取消/异常也走到这里；
            # 只还原「还是本轮换上去的那个」——用户轮中手动换模型时不回滚）
            if routed_now is not None and agent.provider is routed_now:
                agent.provider = routed_prev
            if routed_now is not None:
                self._release_turn_providers([routed_now], keep=(routed_prev,))
            # 轮被取消/异常中止时，未决权限的决策永远不会到来（CancelledError
            # 从 pending.wait() 穿透，agent 侧不再产出 resolved 事件）：这里补发
            # 带 cancelled 语义的 resolved，让前端把可能残留的确认卡收掉——否则
            # 卡上按钮的投递只会静默拿到 delivered=false，切标签还会把死卡重新
            # 弹出。正常结束路径 _perm_pending 已空（决策路径各自发过 resolved），
            # 一条不发；emit_ev 会顺带摘掉对应条目，故用快照遍历、只清一次。
            for rid in list(_perm_pending):
                try:
                    await emit_ev(
                        PermissionResolved(request_id=rid, decision="cancelled").model_dump()
                    )
                except asyncio.CancelledError:
                    asyncio.current_task().uncancel()
                except Exception:  # noqa: BLE001 - 客户端断开不影响收尾
                    pass
            # 再冲刷流式增量缓冲：取消/异常/正常结束三条路径都要走到，
            # 否则最后几十毫秒的正文会丢在前端（用户看到回答缺尾）。
            # 收尾期的重复取消在此吞掉（首个取消已在上面捕获、语义已定为
            # stopped；与下方落库 shield 的 uncancel 取向一致），保证后面的
            # 落库/用量/交棒总是执行，排队中的消息不会永远等不到响应。
            try:
                await merger.aclose()
            except asyncio.CancelledError:
                asyncio.current_task().uncancel()
            except BaseException:  # noqa: BLE001 - 客户端断开不影响收尾
                pass

        # ---- 收尾必须严格串行：先落库，再交棒给排队轮，否则两条持久化
        # ---- 并发会撞 messages(session_id, seq) 唯一约束。
        # 取消保护：用户点停止时，已产出的消息仍要落库、运行位必须释放；
        # shield 让落库在后台继续，CancelledError 被捕获后先收割后台落库再走
        # 完收尾返回 stopped 结果（二次取消下「先落库再交棒」同样成立，见下），
        # 避免 send() 抛异常导致 runtime.run_task 悬挂、后续消息误入死队列。
        # 压缩摘要不落库：它是引擎生成的上下文产物，入库会被当成一条 user 消息
        # 回传前端（渲染成假的用户气泡），也会进 FTS 与会话导出；不落库的代价
        # 只是重启后长会话重新压缩一次，比上面两类污染便宜得多。
        new_msgs_all = [
            m for m in agent.history
            if m.id not in pre_ids and not is_compaction_summary(m)
        ]
        _stamp_turn_estimate(new_msgs_all, turn_estimate, turn_t0)
        # new_msgs 保持「本轮全部新增」口径，后面的摘要/标题/记忆提炼都还用；
        # 落库只写还没在途写过的那些（已在途增量落库的消息回写一次元数据）。
        new_msgs = new_msgs_all
        to_persist = [m for m in new_msgs_all if m.id not in persisted_ids]
        for m in new_msgs_all:
            if m.id in persisted_ids and m.role == "assistant" and m.estimate is not None:
                try:
                    await self.store.replace_message(sid, m)
                except Exception:  # noqa: BLE001 - 元数据回写失败不拖垮收尾
                    pass
        # 落库建成显式任务（shield 的内层）：被停时它在后台继续跑完；登记进
        # _persist_tasks 供 delete_session 等待（等主轮任务等不到后台落库）。
        persist_task = asyncio.get_running_loop().create_task(
            self._persist_turn(sid, to_persist, summary_src=new_msgs)
        )
        self._persist_tasks[sid] = persist_task
        persist_task.add_done_callback(
            lambda t, _sid=sid: self._persist_tasks.pop(_sid, None)
            if self._persist_tasks.get(_sid) is t else None
        )
        try:
            await asyncio.shield(persist_task)
        except asyncio.CancelledError:
            stopped = True
            asyncio.current_task().uncancel()
            # 二次取消（用户连点两次停止）落在这里时，persist_task 已在后台
            # 独立继续跑。不等它完成就走 _pop_next 交棒，交棒轮自己的落库会
            # 与这份后台旧落库并发「SELECT MAX(seq)+1 → INSERT」，撞
            # messages(session_id, seq) 唯一约束——要么交棒轮报 IntegrityError，
            # 要么被停轮的 INSERT 失败被 shield 回调静默吞掉、消息无声丢失。
            # 所以必须先收割后台落库、再继续收尾与交棒；截止时间兜底防卡死，
            # 等待期间再来的取消同样吞掉（语义已定为 stopped）。
            deadline = time.monotonic() + _PERSIST_HARVEST_TIMEOUT
            while not persist_task.done() and time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(persist_task),
                        timeout=max(0.05, deadline - time.monotonic()),
                    )
                except asyncio.CancelledError:
                    asyncio.current_task().uncancel()
                except TimeoutError:
                    pass  # 到点再看一眼：deadline 兜底，不无限等
            if not persist_task.done():
                # 落库卡死（如库被锁）不能拖着收尾无限等：取消后台落库，
                # 宁可这轮消息不落库也不让排队轮永远起不来
                persist_task.cancel()
                obs_warning(
                    "persist",
                    f"turn persist harvest timed out, cancelled sid={sid}",
                    session_id=sid,
                )
            elif not persist_task.cancelled():
                exc = persist_task.exception()
                if exc is not None:
                    # 收割异常（不再被 shield 回调静默吞掉）：停止语义下不上抛，
                    # 只记日志——上抛会把「停止」变成报错；消息仍在 agent 历史，
                    # 下次正常落库可补上
                    obs_warning(
                        "persist",
                        f"turn persist failed after cancel sid={sid}: {exc!r}",
                        session_id=sid,
                    )

        # 对抗报告的消息序号在落库时才回填（AssistantMessage 事件先于落库发出，
        # 事件里 seq 为空）：收尾后从本轮新增消息里取回来随响应下发，前端据此
        # 给「转为团队工单」按钮补上服务端校验用的锚点。
        adv_seq = 0
        if adv_meta is not None:
            for m in reversed(new_msgs):
                if m.role == "assistant" and m.adversarial and m.seq:
                    adv_seq = int(m.seq)
                    break

        # 运行中心终态收口：取消 → cancelled；异常 → failed（带可重试标记）；
        # 引擎级错误（ErrorEvent / TurnFinished stop_reason=error）→ failed；
        # 正常 → completed。团队/圆桌/对抗轮也按同一口径（meta.status 已带取消）。
        # 只有“真的没跑完”的运行才允许重发（RETRYABLE_STATES 另在 retryable_run 强校验）。
        run_final_state = "completed"
        if stopped:
            run_final_state = "cancelled"
        elif turn_exc is not None:
            run_final_state = "failed"
        elif "cancelled" in (
            (tm_meta or {}).get("status"),
            (rt_meta or {}).get("status"),
            (adv_meta or {}).get("status"),
        ):
            run_final_state = "cancelled"
        elif turn_fail:
            # Agent 已把 Provider 异常吞成 ErrorEvent：外层无异常但本轮确实失败
            run_final_state = "failed"
        # 工具报错不单独判失败：模型可能靠后续调用或文字回答救回来。
        # （旧分支 tool_errors and not tool_calls 永远不成立——两者在同一处累加）
        run_error = (
            (type(turn_exc).__name__ + ": " + str(turn_exc)[:200]) if turn_exc
            else (turn_fail.get("error") or None)
        )
        self.runs.update(
            run_id, state=run_final_state,
            error=run_error,
            tools_used=turn_stats["tool_calls"],
        )

        # 用量记录：本轮实际消耗的输入/输出 tokens（含缓存命中数，费用按缓存价拆算）
        # 服务 / 模型取这一轮 Agent 实际持有的 provider：会话级模型覆盖、产线级
        # 模型（定时任务）都是独立 provider 实例，不能记成全局「当前服务」。
        try:
            prov = getattr(agent, "provider", None)
            # 轮级质量口径（v14）：引擎级失败（异常 / ErrorEvent / stop_reason=error）
            # 算 failed；用户主动停止不算——那是人走的，不是模型跑挂的。
            turn_failed = bool(turn_exc) or bool(turn_fail)
            await self.store.add_usage(
                sid,
                getattr(prov, "name", "") or self.provider_name,
                getattr(prov, "model", "") or self.provider_model,
                agent.total_in_tokens - tin0, agent.total_out_tokens - tout0,
                agent.total_cached_tokens - tcached0,
                tool_calls=turn_stats["tool_calls"],
                tool_errors=turn_stats["tool_errors"],
                failed=1 if turn_failed else 0,
            )
        except Exception:  # noqa: BLE001
            pass
        # 预算告警：越线档位经渠道推一条（内部不抛、推送 spawn_bg 不阻塞收尾）
        await self._check_budget_alerts()

        # 结构化耗时记录：回答「这轮为什么慢」
        # （总耗时 / 工具次数与最慢工具 / 权限等待——这几项最容易看出卡在哪一段）。
        try:
            obs_info(
                "turn",
                f"turn finished sid={sid}",
                session_id=sid,
                duration_ms=int((time.monotonic() - turn_t0) * 1000),
                stopped=stopped or None,
                roundtable=bool(rt_meta) or None,
                adversarial=bool(adv_meta) or None,
                team=bool(tm_meta) or None,
                tool_calls=turn_stats["tool_calls"] or None,
                tool_ms=turn_stats["tool_ms"] or None,
                tool_errors=turn_stats["tool_errors"] or None,
                dup_tool_calls=turn_stats["dup_tool_calls"] or None,
                slowest_tool=turn_stats["slowest_tool"] or None,
                slowest_tool_ms=turn_stats["slowest_tool_ms"] or None,
                permission_waits=turn_stats["permission_waits"] or None,
                permission_ms=turn_stats["permission_ms"] or None,
                in_tokens=agent.total_in_tokens - tin0 or None,
                out_tokens=agent.total_out_tokens - tout0 or None,
                # 只记类名会丢掉「为什么失败」：附上前 200 字；Agent 内部
                # 吞掉的失败（ErrorEvent / stop_reason=error）带错误类别
                error=(
                    f"{type(turn_exc).__name__}: {str(turn_exc)[:200]}"
                    if turn_exc else (
                        ": ".join(
                            x for x in (
                                turn_fail.get("category"), turn_fail.get("error")
                            ) if x
                        )[:300] or None
                    )
                ),
            )
        except Exception:  # noqa: BLE001 - 日志不偿能影响主流程
            pass

        # 首轮对话：后台用模型生成简短标题（8-12 字），替换首行截断文本
        if wants_title and self.provider is not None and sid not in self._titling:
            first_user = text
            first_reply = next(
                (m.text.strip() for m in new_msgs if m.role == "assistant" and m.text.strip()), ""
            )
            if first_user:
                spawn_bg(
                    self._auto_title(sid, first_user, first_reply)
                )

        # 记忆二期：轮次收尾后抽「值得长期记住」的候选（开关默认关；
        # spawn_bg + 失败静默 + 按会话单飞都在方法内，绝不影响主流程）
        self.schedule_turn_distill(sid, new_msgs)

        # 本轮改动了文件 → 存检查点（落盘 + 内存索引）；结果里带给前端做「撤销本轮改动」。
        # 检查点库按会话所属项目取（跨项目分屏列的快照落它自己项目的目录树，
        # 不与引擎当前项目混放——与 _bind_project 的「检查点跟项目走」同口径）。
        ckpt_store = runtime.ctx.checkpoints if runtime.ctx is not None and runtime.ctx.own \
            else self.checkpoints
        checkpoint = await asyncio.to_thread(
            ckpt_store.save, sid, dict(runtime.recorder.pre)
        )
        runtime.recorder.reset()

        def _pop_next() -> bool:
            """启动下一个排队轮，把"运行中"的接力棒递给它；有交棒返回 True。"""
            if self._base_queue:
                # 会话懒创建期间排进基底队列的消息，并入本会话队列
                runtime.queue.extend(self._base_queue)
                self._base_queue.clear()
            if not runtime.queue:
                return False
            nxt = runtime.queue.pop(0)
            runtime.run_task = asyncio.get_running_loop().create_task(
                self._run_queued(nxt, runtime)
            )
            return True

        # queue_updated 是前端运行状态的唯一事实源：
        # pending = 仍排队轮数 +（1 如果刚接棒了一轮），归零才真正空闲
        handed_off = _pop_next()
        try:
            await emit_ev(
                QueueUpdated(pending=len(runtime.queue) + (1 if handed_off else 0)).model_dump()
            )
        except Exception:  # noqa: BLE001 - 客户端断开不影响收尾
            pass
        # 收尾运行态广播到其他连接：发起连接由上方 emit_ev 覆盖（带 seq、可补帧），
        # 排除之避免「任务完成」通知双响；发起连接已断开时它不在 ws_emitters，无碍
        self._broadcast_ws(
            {
                **QueueUpdated(
                    pending=len(runtime.queue) + (1 if handed_off else 0),
                    session_id=sid,
                ).model_dump()
            },
            exclude=getattr(emit, "_source_emit", emit),
        )

        result = {
            "done": True,
            "stopped": stopped,
            "session_id": sid,
            "plan_mode": plan_mode,
            "roundtable": rt_meta,
            "adversarial": adv_meta,
            "adversarial_seq": adv_seq,
            "team": tm_meta,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
            "context_detail": self._context_detail(agent),
        }
        if checkpoint is not None:
            result["checkpoint"] = checkpoint
        # 没有交棒（无排队轮）才释放运行标志
        if runtime.run_task is asyncio.current_task():
            runtime.run_task = None
        # 兜底：交棒之后、释放标志前后又进来了排队请求 → 这里再拉起一次并广播
        if runtime.queue and (runtime.run_task is None or runtime.run_task.done()):
            handed_off2 = _pop_next()
            try:
                await emit_ev(
                    QueueUpdated(
                        pending=len(runtime.queue) + (1 if handed_off2 else 0)
                    ).model_dump()
                )
            except Exception:  # noqa: BLE001
                pass
            self._broadcast_ws(
                {
                    **QueueUpdated(
                        pending=len(runtime.queue) + (1 if handed_off2 else 0),
                        session_id=sid,
                    ).model_dump()
                },
                exclude=getattr(emit, "_source_emit", emit),
            )
        if turn_exc is not None:
            raise turn_exc  # 收尾已做完，原样上抛交给 WS 层
        return result

    async def _run_queued(self, item: QueuedTurn, runtime: SessionRuntime) -> None:
        # 状态由 _run_turn_pipeline finally 里的 queue_updated 统一播报，这里不重复发
        try:
            item.resolve(await self._run_turn_pipeline(
                item.text, item.emit, item.plan_mode,
                roundtable=item.roundtable, members_params=item.members,
                images=item.images, runtime=runtime, refs=item.refs,
                compare=item.compare, debate_rounds=item.debate_rounds,
                chair_answers=item.chair_answers, team=item.team,
                adversarial=item.adversarial,
                director_mode=item.director_mode, director=item.director,
                team_template=item.team_template,
                template=item.template, grant=item.grant,
                local=item.local,
            ))
        except Exception as e:  # noqa: BLE001 - 错误要送回等待中的请求
            item.fail(e)

    def cancel_run(self, session_id: str | None = None) -> bool:
        """停止指定会话（缺省 = 活动会话）正在跑的 turn。"""
        rt = self.runtimes.get(session_id) if session_id else None
        task = (rt.run_task if rt else None) or (self._run_task if not session_id else None)
        if task and not task.done():
            task.cancel()
            # 取消时立刻清未决权限：权限弹窗可能还留在屏上，但这一轮已经结束，
            # 残留的 request_id 再被投递会「成功」（前端以为决策生效）。
            # 安全审查 M12——Agent 侧还有一层 finally 兜底
            agent = getattr(rt, "agent", None) if rt is not None else None
            if agent is None:
                agent = getattr(self, "agent", None)
            if agent is not None:
                try:
                    agent.clear_pending()
                except Exception:  # noqa: BLE001 - 清理失败不挡停止
                    pass
            return True
        return False
