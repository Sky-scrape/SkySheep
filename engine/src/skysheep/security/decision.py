r"""权限门的单一决策源：``decide()`` 是全引擎唯一的权限判定实现。

背景：``PermissionGate.needs_confirm``（并发只读批收集期预检）与
``PermissionGate.authorize``（串行路径的真正授权）曾经各自维护一份判定序，
靠注释与对照测试钉住同步——历史上已经漂过两次（memory_write 事故 FINDING 2、
出站密钥 H1），都是「第二份判定」藏在一个两千行文件的两个方法里被从侧面绕开。
本模块把两函数共用的判定序抽成一份纯同步、无副作用的实现：两边都改为消费
``decide()`` 的结论，结构上不可能再各写各的。

约束：
- 运行期零导入 ``gate.py``（仅 TYPE_CHECKING 引用类型）：gate.py 单向 import
  decision，无循环导入；gate 状态全部经 duck-typed 方法定（``_egress_scan`` /
  ``_extra_confirm_required`` / ``_write_hits_engine_home`` /
  ``_write_is_actually_delete`` / ``_write_target_inside_workdir`` /
  ``_matching_rule`` 与 ``auto_accept_write`` / ``auto_accept_all`` 两个档位
  属性），不 isinstance 任何具体门类——四个真子类门与三个包装门都可直接喂进来。
- 纯同步、无 await、无 IO 副作用：``needs_confirm`` 全程同步；authorize 在进
  ``_build_pending`` 前无 await（Mods 命中说明与命中判定两读共处同步段的并发
  注记保持成立）。
- 有意不携带的字段：arg_text / diff / scope / reversible——``_build_pending``
  是这些「预览所需字段」的唯一产地，决策结构只搬运 authorize 完成收尾必需的
  判定产物，避免出现第二个 ``tool.arg_text`` 拼接点。
- 白名单记账（record_rule_hit）与 egress 预拒绝（``_egress_denied``）都留在
  authorize 侧：decide 只供给料。防止未来有人把记账塞进决策函数，让预检产生
  写库副作用。

reason 是稳定机读码，仅供日志 / 测试 / 覆写门分流，不进 PermissionRequest
事件流（前端契约不动）；若未来要透出「为什么没放行」，应另行走
``PermissionGate._note_for`` 文案，不把机读码直接拼进 note。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ..tools.base import Safety, Tool

if TYPE_CHECKING:
    from .gate import PermissionGate, WhitelistRule


class Tier(StrEnum):
    """决策档位。

    - ALLOW：免确认放行（某个自动放行分支或 force_allow 命中）；
    - CONFIRM：逐次确认（authorize 落 ``_build_pending``）；
    - DENY：预拒绝（egress 已知密钥；authorize 直接给已 resolve(DENY) 的
      pending，不给确认卡）。
    """

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class GateDecision:
    """``decide()`` 的纯值结论：needs_confirm 与 authorize 各取所需。

    Attributes:
        tier: 三档决策（见 :class:`Tier`）。
        reason: 稳定机读码，与 authorize 原分支一一对应（供日志/测试锚定）。
        rule: 白名单分支命中的规则（仅 ``whitelist`` 分支非 None）——authorize
            用它做命中记账（record_rule_hit）；needs_confirm 忽略它。
        scan: egress 扫描的原始结果（仅 ``egress_known_key`` 分支非 None）——
            authorize 构造 ``_egress_denied`` 时复用，免二次扫描。
        extra_required: Mods 收紧声明命中（``_extra_confirm_required`` 结果），
            authorize 传给 ``_build_pending`` 拼用户可见的 mod 说明行。
        engine_home_write: 引擎主目录写入守卫命中，authorize / 覆写门直接取用，
            避免对守卫的第三次调用。
    """

    tier: Tier
    reason: str
    rule: WhitelistRule | None = None
    scan: dict | None = None
    extra_required: bool = False
    engine_home_write: bool = False


def decide(
    gate: PermissionGate,
    tool: Tool,
    input_dict: dict,
    *,
    force_pending: bool = False,
    force_allow: bool = False,
) -> GateDecision:
    """对一次工具调用给出三档决策。分支序与 authorize 的原判定序同构。

    - 第 1 步 egress 预拒对**全工具域**扫描（WRITE/DANGEROUS 也扫，与原
      authorize 顶端一致）：needs_confirm 侧由此闭合「非 READONLY 域预检
      不扫出站密钥」的历史缺口（分歧 D1，取严）——批预检命中报 True 断批，
      回退串行由 authorize 预拒绝落 DENY。scan 为 None（非开放网络只读、
      无字符串参数、防线故障降级）按未扫描处理，不扩大打击面。
    - force_pending（Claude 兼容模式 tool.check=ask）：跳过全部自动放行，
      且不提供「总是允许」（always_rule 由 authorize 侧传 None）。两者同现
      时 force_pending 优先（更保守）。
    - force_allow（tool.check=allow）：跳过档位 / 白名单，硬守卫保留
      （引擎主目录写入、删除语义、Mods 收紧）。
    - ``_write_is_actually_delete`` 的重复调用与原 authorize 相同（同一输入
      的纯路径解析，无副作用）。

    取代原先散落在 needs_confirm / authorize 两处的手写判定序；两函数对
    caller 的签名与契约不变，结论由本函数单一供给。
    """
    # 1. egress 预拒（原 authorize A1，先于一切放行分支）：已知密钥值命中
    #    预拒绝——参数里是真实凭据，确认弹窗只会把它再展示一遍，拦截是唯一
    #    正确动作；防线自身故障按未扫描降级，绝不挡执行。
    scan = gate._egress_scan(tool, input_dict)
    if scan is not None and not scan["ok"]:
        return GateDecision(Tier.DENY, "egress_known_key", scan=scan)
    # 2. force_pending（原 A2）：Mod 点名 ask，必须逐次确认
    if force_pending:
        return GateDecision(Tier.CONFIRM, "force_pending")
    # 3. Mods 收紧声明（原 A3）：命中即跳过下方全部自动放行分支——即使
    #    READONLY 短路 / 完全访问档 / 自动允许写入档 / 白名单本会放行，
    #    被 Mod 点名的工具一律回落逐次确认（只收紧，不放行）
    extra = gate._extra_confirm_required(tool, input_dict)
    if extra:
        return GateDecision(Tier.CONFIRM, "mods_extra_confirm", extra_required=True)
    # 4. 引擎主目录写入守卫（原 A4）：先于 READONLY 短路（memory_write 名义
    #    READONLY 却写注入所有会话 system prompt 的全局记忆，FINDING 2）
    engine_home = gate._write_hits_engine_home(tool, input_dict)
    # 5. force_allow（原 A5）：硬性守卫保留，Mod 也不得放开
    if force_allow:
        if not engine_home and not gate._write_is_actually_delete(tool, input_dict):
            return GateDecision(Tier.ALLOW, "force_allow")
        return GateDecision(
            Tier.CONFIRM, "force_allow_guards",
            extra_required=extra, engine_home_write=engine_home,
        )
    # 6. READONLY 短路（原 A6）：extra 已在步 3 拦下，not extra 恒真，
    #    保留条件仅为与原 authorize 逐字同构
    if tool.safety == Safety.READONLY and not engine_home and not extra:
        return GateDecision(Tier.ALLOW, "readonly")
    # 7. 完全访问档（原 A7）：用户显式选择的「一切放行」，引擎主目录写入
    #    也在其内——与本守卫是同一语义的两面，不是漏挡（审计 L01 口径）；
    #    不想让引擎数据目录被无确认改写，就别开该档。Mods 的收紧声明仍然
    #    压得住它。
    if gate.auto_accept_all and not extra:
        return GateDecision(Tier.ALLOW, "auto_accept_all")
    # 8. 自动允许写入档（原 A8）：只放行 WRITE 级，且目标必须确认在工作
    #    目录内；高危（执行命令）、目录外写入、「名义是移动实为删目录」
    #    仍逐次确认
    if (
        gate.auto_accept_write
        and tool.safety == Safety.WRITE
        and gate._write_target_inside_workdir(tool, input_dict)
        and not gate._write_is_actually_delete(tool, input_dict)
        and not extra
    ):
        return GateDecision(Tier.ALLOW, "auto_accept_write")
    # 9. 白名单（原 A9）：命中且非删除语义、非引擎主目录、非 Mods 命中才
    #    放行。rule 随结构带回，authorize 据此记账；needs_confirm 忽略。
    rule = gate._matching_rule(tool, tool.arg_text(input_dict))
    if (
        rule is not None
        and not gate._write_is_actually_delete(tool, input_dict)
        and not engine_home
        and not extra
    ):
        return GateDecision(Tier.ALLOW, "whitelist", rule=rule, engine_home_write=engine_home)
    # 10. 兜底逐次确认（原 A10），理由细分供日志与测试锚定
    if gate._write_is_actually_delete(tool, input_dict):
        return GateDecision(
            Tier.CONFIRM, "delete_semantics",
            extra_required=extra, engine_home_write=engine_home,
        )
    if engine_home:
        return GateDecision(Tier.CONFIRM, "engine_home_write", extra_required=extra)
    return GateDecision(Tier.CONFIRM, "default_confirm", extra_required=extra)
