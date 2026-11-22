"""用量统计、协作成本看板与轮次耗时诊断。

D1 会话与运行时域（backend.py Mixin 拆分批 5b 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

三段职责：
- usage.stats（usage_stats）：按天 / 会话 / 服务聚合 token 与费用（scope
  口径；B14：远端连接只认项目口径、无项目态不下发 by_session）；
- 协作成本看板（usage_collab_report / usage_pricing_set / _fold_usage_cost）：
  按会话与协作形态聚合。跨域契约（设计 §5 次级风险）：形态判定只读消息落库
  JSON 的 `roundtable` / `adversarial` 槽与团队摘要的 `mode:"team"` 标记
  （D2 协作落库面的既有形状，见 backend.py 的 _team_meta 与 messages.py），
  不调用任何 D2 方法；
- 轮次诊断（turn_breakdown）：读 obs.py 的 `ev=turn` 结构化行。

类属性 `_MARK_ADVERSARIAL` / `_MARK_ROUNDTABLE` / `_MARK_TEAM` 是不可变 SQL
片段，随方法搬入本类；本 Mixin 无 __init__、无 D1 自有字段（读
self.store / self.cfg / self.project）。

轮次日志的模块级件随方法落位本文件（沿批 5a checkpoints 的随迁先例）：
`TURN_LOG_LIMIT_DEFAULT` / `TURN_LOG_LIMIT_MAX`（turn_breakdown 的形参默认值
在导入期求值，必须与定义同模块）、`_LOG_TS_RE` / `_log_line_ts` /
`_turn_int` / `_turn_row` / `_load_turn_rows`。backend.py 以 import 转发其中
五个（`TURN_LOG_LIMIT_DEFAULT` / `TURN_LOG_LIMIT_MAX` / `_log_line_ts` /
`_turn_row` / `_load_turn_rows`；`_LOG_TS_RE` / `_turn_int` 只在方法体内使用，不转发）
——tests/test_turn_breakdown.py 与 tests/test_app_file_logging.py 从
skysheep.server.backend 取用的既有锚点不变。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from ...config import ConfigError, load_config, set_usage_pricing_in_config, skysheep_home
from ...obs import parse_structured
from ...textio import read_text_file

logger = logging.getLogger("skysheep.security")

# ---- 轮次诊断模块级件（随 turn_breakdown 自 backend.py 迁入） ----

# ---- 轮次诊断（审查页）：从桌面日志的结构化行里取回每轮耗时拆解 ----

# diagnostics.turn_breakdown 的 limit 缺省与上限（上限挡住误传的大数，
# 避免一次拖回整本日志的历史轮次）
TURN_LOG_LIMIT_DEFAULT = 20
TURN_LOG_LIMIT_MAX = 200

# 桌面日志行首的时间戳（desktop.py _setup_logging 的 %(asctime)s 默认格式：
# 「2026-09-21 19:30:01,123」本地时间）。ev=turn 的 JSON 里没有时间字段，
# 时间只能从行首取。
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:,(\d{3}))?")


def _log_line_ts(line: str) -> float | None:
    """行首 asctime → epoch 秒（本地时区，与写入侧同一口径）；解析不了返回 None。"""
    m = _LOG_TS_RE.match(line)
    if m is None:
        return None
    try:
        base = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return base.timestamp() + (int(m.group(3)) / 1000 if m.group(3) else 0)


def _turn_int(fields: dict, key: str) -> int | None:
    """结构化字段 → int；缺失或类型不对返回 None（旧日志行/手改日志都容忍）。"""
    v = fields.get(key)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _turn_row(line: str, fields: dict) -> dict:
    """一条 ev=turn 日志行 → 前端一行的诊断数据。字段缺失一律 None。"""
    stopped = fields.get("stopped")
    return {
        "ts": _log_line_ts(line),
        "duration_ms": _turn_int(fields, "duration_ms"),
        "tool_calls": _turn_int(fields, "tool_calls"),
        "tool_ms": _turn_int(fields, "tool_ms"),
        "tool_errors": _turn_int(fields, "tool_errors"),
        "dup_tool_calls": _turn_int(fields, "dup_tool_calls"),
        "slowest_tool": str(fields["slowest_tool"]) if fields.get("slowest_tool") else None,
        "slowest_tool_ms": _turn_int(fields, "slowest_tool_ms"),
        "permission_waits": _turn_int(fields, "permission_waits"),
        "permission_ms": _turn_int(fields, "permission_ms"),
        "in_tokens": _turn_int(fields, "in_tokens"),
        "out_tokens": _turn_int(fields, "out_tokens"),
        "stopped": bool(stopped) if stopped is not None else None,
        "error": str(fields["error"]) if fields.get("error") else None,
    }


def _load_turn_rows(log_path: Path, sid: str) -> list[dict]:
    """读日志文件 → 过滤出目标会话的 ev=turn 行（同步函数，调用方 to_thread）。

    只读当前 desktop.log，不追轮转旧文件（desktop.log.*）；文本走 textio
    读（编码安全），解析不了的行直接跳过。
    """
    try:
        text = read_text_file(log_path).text
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        fields = parse_structured(line)
        if not fields or fields.get("ev") != "turn":
            continue
        if fields.get("session_id") != sid:
            continue
        rows.append(_turn_row(line, fields))
    return rows


class UsageMixin:
    """用量统计 / 协作成本 / 轮次诊断（D1 批 5b 外迁件）。

    方法自 backend.py 逐字搬入；状态全部来自主类共享 self（无 __init__）。
    """

    async def usage_stats(self, params: dict, *, local: bool = True) -> dict:
        """用量统计：按天/会话/服务聚合 + 按 provider 单价估算费用。

        统计口径由 ``scope`` 决定：

        - ``all``：全部项目 + 快聊（本机用量页默认）——用量页是「我花了多少
          tokens」的个人报表，新建项目里还没有会话时按项目口径会整页显示 0；
        - ``project``：只看当前项目（无项目态退化成 all，与历史行为一致）；
        - 缺省 ``auto``：有项目态按项目、无项目态按全部（协议层旧口径，CLI /
          旧前端不传 scope 时行为不变）。

        远端连接（``local=False``）不放开 ``all``：by_session 带会话标题，跨项目
        聚合会借远程接口泄露（安全审查 B14）；同样原因，无项目态的全局口径不下发
        by_session（本机显式选「全部」时才下发）。每日预算护栏用的 usage_today
        始终是全局口径（预算本身是全局设置）。
        """
        params = params or {}
        days = min(90, max(1, int(params.get("days", 14))))
        scope = str(params.get("scope") or "auto")
        if scope not in ("all", "project"):
            scope = "auto"
        if not local:
            scope = "auto"
        use_global = scope == "all" or self.project is None
        if use_global:
            st = await self.store.usage_stats(days)
            if not (local and scope == "all"):
                # 无项目态 / 远端的全局口径：明细不给（B14：防跨项目标题枚举）
                st["by_session"] = []
        else:
            st = await self.store.usage_stats(days, project_id=self.project.id)
        prices = {
            name: {
                "price_in": pc.price_in,
                "price_out": pc.price_out,
                "price_cache": pc.price_cache,
            }
            for name, pc in self.cfg.providers.items()
        }
        cost = 0.0
        for row in st["by_provider"]:
            pr = prices.get(row["provider"] or "", {})
            it, ot = row["it"] or 0, row["ot"] or 0
            # 缓存命中价（>0）时把命中部分从输入价里拆出来单算：
            # 多数服务命中提示词缓存的输入便宜得多（DeepSeek 约输入价 1/10）
            cached = row.get("cached") or 0
            price_cache = pr.get("price_cache", 0.0)
            if price_cache > 0 and cached > 0:
                cost += max(0, it - cached) / 1e6 * pr.get("price_in", 0.0)
                cost += cached / 1e6 * price_cache
            else:
                cost += it / 1e6 * pr.get("price_in", 0.0)
            cost += ot / 1e6 * pr.get("price_out", 0.0)
        total_in = sum(r["it"] or 0 for r in st["by_provider"])
        total_out = sum(r["ot"] or 0 for r in st["by_provider"])
        total_cached = sum(r.get("cached") or 0 for r in st["by_provider"])
        return {
            "days": days,
            # 实际生效的统计口径（后端可能把 all 收敛成项目口径：远端连接 /
            # 没有打开项目）——前端据此回填切换按钮，别让按钮说谎
            "scope": "all" if use_global else "project",
            "project_name": self.project.name if self.project is not None else "",
            "by_day": st["by_day"],
            "by_session": st["by_session"],
            "by_provider": st["by_provider"],
            "total_in": total_in,
            "total_out": total_out,
            "total_cached": total_cached,
            # 真实去重会话数：无项目态与 by_session 被截断时它才是准确口径
            "session_count": st.get("session_count", 0),
            # 连续天数 / 最长聊天（设置·用量页的两块统计；无数据为 0）
            "streak_current": st.get("streak_current", 0),
            "streak_longest": st.get("streak_longest", 0),
            "longest_chat_seconds": st.get("longest_chat_seconds", 0.0),
            # 轮级质量口径（轮成功率 / 工具报错率；无数据为 None，前端不展示）
            "quality": st.get("quality"),
            "cost": round(cost, 4),
            "has_price": any(p["price_in"] or p["price_out"] for p in prices.values()),
            # 每日预算与当日用量：用量页据此显示「今日已用 / 预算」，0 = 未设上限
            "budget": self.cfg.daily_token_budget,
            "today": await self.store.usage_today(),
        }

    # ---- 协作成本看板（usage.collab_report / usage.pricing_set） ----

    # usage_log 没有协作形态列，形态只能从会话消息的落库 JSON 判（不硬造）：
    # 圆桌 / 团队 / 对抗的 meta 随 assistant 消息持久化（Message.roundtable /
    # adversarial，见 messages.py 与 backend.py 里圆桌 / 团队 / 对抗收尾的落库代码），
    # 普通消息的 JSON 里这些槽是 null——所以 LIKE 必须带 "{"（对象开头），
    # 只匹配 '"roundtable":' 会把每条普通消息都误判成圆桌。团队摘要复用
    # roundtable 槽（_team_meta：mode == "team"），对抗用独立 adversarial 槽。
    # 判定是会话级：一个会话混用多种形态时按 对抗 > 团队 > 圆桌 的固定优先级
    # 归类（现有字段分不出逐轮归属，宁可整会话归一类也不硬拆），其余归「普通」。
    _MARK_ADVERSARIAL = "content LIKE '%\"adversarial\":{%'"
    _MARK_ROUNDTABLE = "content LIKE '%\"roundtable\":{%'"
    _MARK_TEAM = ("content LIKE '%\"roundtable\":{%' AND"
                  " content LIKE '%\"mode\":\"team\"%'")

    @staticmethod
    def _fold_usage_cost(it: int, ot: int, cached: int,
                         prices: tuple[float, float, float]) -> float:
        """按价目（输入 / 输出 / 缓存命中，元 / 百万 tokens）折算一组用量。

        缓存命中价（>0）时把命中部分从输入价里拆出来单算（与 usage_stats 同式）：
        多数服务命中提示词缓存的输入便宜得多（DeepSeek 约输入价 1/10）。
        """
        p_in, p_out, p_cache = prices
        if p_cache > 0 and cached > 0:
            cost = max(0, it - cached) / 1e6 * p_in + cached / 1e6 * p_cache
        else:
            cost = it / 1e6 * p_in
        return cost + ot / 1e6 * p_out

    async def usage_collab_report(self, params: dict, *, local: bool = True) -> dict:
        """协作成本看板：近 N 天（默认 30）按会话与协作形态聚合 token 与轮次。

        只读。统计口径与 usage.stats 同一套（scope=all|project|auto）：
        用量页「全部」时把快聊与其他项目一起算，「本项目」只看当前项目；
        远端连接（local=False）只认项目口径（B14：会话标题不跨项目下发）。
        费用折算用 [usage_pricing] 价目（providers.<服务名> 覆盖默认价），
        全 0 = 不计价，前端只展示 token。
        「轮次」按用量入账行计：普通会话每轮一行；圆桌 / 团队 / 对抗按成员
        （或角色）逐行入账，行数不是回合数——如实呈现，不做换算。
        """
        params = params or {}
        days = min(90, max(1, int(params.get("days", 30))))
        now = time.time()
        since = now - days * 86400
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        window_start = min(since, midnight)
        scope = str(params.get("scope") or "auto")
        if scope not in ("all", "project"):
            scope = "auto"
        if not local:
            scope = "auto"
        scoped = self.project is not None and scope != "all"
        pz = self.cfg.usage_pricing

        def pricing_block() -> dict:
            return {"price_in": pz.price_in, "price_out": pz.price_out,
                    "price_cache": pz.price_cache}

        def empty() -> dict:
            blank = {"sessions": 0, "turns": 0, "in": 0, "out": 0, "cached": 0, "cost": 0.0}
            keys = ("normal", "roundtable", "team", "adversarial")
            return {
                "days": days, "has_price": False, "pricing": pricing_block(),
                "scope": "project" if scoped else "all",
                "forms": {k: dict(blank) for k in keys},
                "today": {k: dict(blank) for k in keys},
                "by_session": [],
            }

        db = self.store._db
        if db is None:
            return empty()
        try:
            join = (
                " FROM usage_log l JOIN sessions s ON s.id = l.session_id"
                if scoped else " FROM usage_log l LEFT JOIN sessions s ON s.id = l.session_id"
            )
            cond = " WHERE l.ts >= ?" + (" AND s.project_id = ?" if scoped else "")
            # 占位符顺序跟 SQL 文本走：SELECT 里 4 个 CASE 的 midnight，
            # 再 WHERE 里的窗口起点，最后（项目态）才轮到 s.project_id
            args: tuple = (midnight, midnight, midnight, midnight, window_start)
            if scoped:
                args = (*args, self.project.id)
            cur = await db.execute(
                "SELECT l.session_id AS sid, MAX(s.title) AS title,"
                " l.provider AS provider,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot,"
                " SUM(l.cached_tokens) AS ct,"
                " COUNT(*) AS turns, MAX(l.ts) AS last_ts,"
                " SUM(CASE WHEN l.ts >= ? THEN l.in_tokens ELSE 0 END) AS it_td,"
                " SUM(CASE WHEN l.ts >= ? THEN l.out_tokens ELSE 0 END) AS ot_td,"
                " SUM(CASE WHEN l.ts >= ? THEN l.cached_tokens ELSE 0 END) AS ct_td,"
                " SUM(CASE WHEN l.ts >= ? THEN 1 ELSE 0 END) AS turns_td"
                + join + cond + " GROUP BY l.session_id, l.provider",
                args,
            )
            groups = [dict(r) for r in await cur.fetchall()]
            # 形态判定：只扫窗口内有用量的会话（子查询走 idx_usage_ts），
            # 消息内容按标记 LIKE，命中即该会话出现过对应协作形态
            cur = await db.execute(
                "SELECT m.session_id AS sid,"
                f" MAX({self._MARK_ADVERSARIAL}) AS adv,"
                f" MAX({self._MARK_TEAM}) AS team,"
                f" MAX({self._MARK_ROUNDTABLE}) AS rt"
                " FROM messages m WHERE m.session_id IN"
                " (SELECT DISTINCT session_id FROM usage_log WHERE ts >= ?)"
                " GROUP BY m.session_id",
                (window_start,),
            )
            form_by_sid: dict[str, str] = {}
            for r in await cur.fetchall():
                if r["adv"]:
                    form_by_sid[r["sid"]] = "adversarial"
                elif r["team"]:
                    form_by_sid[r["sid"]] = "team"
                elif r["rt"]:
                    form_by_sid[r["sid"]] = "roundtable"
                else:
                    form_by_sid[r["sid"]] = "normal"
        except Exception as e:  # noqa: BLE001 - 查不动（迁移中等）时回空报表，不炸用量页
            logger.warning("协作成本报表查询失败，回空报表：%s", e)
            return empty()

        def price_for(provider: str) -> tuple[float, float, float]:
            entry = pz.providers.get(provider or "")
            if entry is not None:
                return entry.price_in, entry.price_out, entry.price_cache
            return pz.price_in, pz.price_out, pz.price_cache

        def new_bucket() -> dict:
            return {"sessions": set(), "turns": 0, "in": 0, "out": 0,
                    "cached": 0, "cost": 0.0}

        forms = {k: new_bucket() for k in ("normal", "roundtable", "team", "adversarial")}
        today = {k: new_bucket() for k in ("normal", "roundtable", "team", "adversarial")}
        by_session: dict[str, dict] = {}
        for g in groups:
            form = form_by_sid.get(g["sid"] or "", "normal")
            prices = price_for(g["provider"] or "")
            total_cost = self._fold_usage_cost(
                g["it"] or 0, g["ot"] or 0, g["ct"] or 0, prices)
            today_cost = self._fold_usage_cost(
                g["it_td"] or 0, g["ot_td"] or 0, g["ct_td"] or 0, prices)
            for bucket, it, ot, ct, turns, cost in (
                (forms[form], g["it"], g["ot"], g["ct"], g["turns"], total_cost),
                (today[form], g["it_td"], g["ot_td"], g["ct_td"], g["turns_td"], today_cost),
            ):
                bucket["sessions"].add(g["sid"] or "")
                bucket["turns"] += int(turns or 0)
                bucket["in"] += int(it or 0)
                bucket["out"] += int(ot or 0)
                bucket["cached"] += int(ct or 0)
                bucket["cost"] += cost
            row = by_session.setdefault(g["sid"] or "", {
                "sid": g["sid"] or "", "title": g["title"] or "", "form": form,
                "in": 0, "out": 0, "cached": 0, "turns": 0, "cost": 0.0, "last_ts": 0.0,
            })
            row["in"] += int(g["it"] or 0)
            row["out"] += int(g["ot"] or 0)
            row["cached"] += int(g["ct"] or 0)
            row["turns"] += int(g["turns"] or 0)
            row["cost"] += total_cost
            row["last_ts"] = max(row["last_ts"], g["last_ts"] or 0.0)

        def dump(buckets: dict) -> dict:
            return {
                key: {
                    "sessions": len(v["sessions"]), "turns": v["turns"],
                    "in": v["in"], "out": v["out"], "cached": v["cached"],
                    "cost": round(v["cost"], 4),
                }
                for key, v in buckets.items()
            }

        sess_rows = sorted(
            by_session.values(), key=lambda r: r["in"] + r["out"], reverse=True,
        )[:12]
        return {
            "days": days,
            "scope": "project" if scoped else "all",
            "has_price": bool(
                pz.price_in or pz.price_out or pz.price_cache
                or any(e.price_in or e.price_out or e.price_cache
                       for e in pz.providers.values())
            ),
            "pricing": pricing_block(),
            "forms": dump(forms),
            "today": dump(today),
            # B14：带会话标题的明细只给项目态（与 usage.stats 同一口径）
            "by_session": sess_rows if scoped else [],
        }

    async def usage_pricing_set(self, params: dict) -> dict:
        """保存协作成本看板的默认价目（元 / 百万 tokens），落 config.toml 后热生效。

        本机专属（dispatch 注册 local_only）：写的是配置文件。providers 子表
        （按服务覆盖）不在此改，界面只编辑默认价目。
        """
        picked: dict[str, float | None] = {}
        for key, label in (("price_in", "输入价"), ("price_out", "输出价"),
                           ("price_cache", "缓存命中价")):
            raw = params.get(key)
            if raw is None or raw == "":
                picked[key] = None
                continue
            try:
                picked[key] = float(raw)
            except (TypeError, ValueError):
                raise RuntimeError(f"{label}需要一个数字（元 / 百万 tokens）") from None
        try:
            set_usage_pricing_in_config(**picked)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()  # 与其它设置项同一姿态：保存后重载即热生效
        pz = self.cfg.usage_pricing
        return {
            "price_in": pz.price_in, "price_out": pz.price_out,
            "price_cache": pz.price_cache,
            "has_price": bool(pz.price_in or pz.price_out or pz.price_cache),
        }

    # ---- 轮次诊断（审查页：「这一轮为什么慢」的 UI 兑现） ----

    async def turn_breakdown(self, session_id: str = "", limit: int = TURN_LOG_LIMIT_DEFAULT) -> dict:
        """从桌面日志的结构化行里取回本会话最近 N 轮的耗时拆解。

        数据源是 obs.py 轮末记的 ``ev=turn`` 行（~/.skysheep/logs/desktop.log）：
        只读当前文件、不追轮转旧文件；日志文件不存在（如 CLI 模式没接文件日志）
        返回空列表而不是报错。字段缺失容忍——旧版本日志行没有 slowest_tool 等
        字段时相应位置就是 null。

        session_id 由调用方解析完毕再传入（本机=前端点选的会话；远程=连接绑定
        会话，未绑定为空串），这里不再二次回退全局活动会话：空串一律按「无授权
        会话」直接回空结果——否则未绑定的远程连接会读到全局指针当前指着的会话
        的诊断（耗时/工具名/token），且边界随本机窗口切会话漂移（安全审查 B 族，
        与 list_checkpoints 的空串口径一致）。前端审查页恒传 currentSessionId，
        本机缺省路径没有真实调用方。
        """
        sid = str(session_id or "").strip()
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = TURN_LOG_LIMIT_DEFAULT
        n = max(1, min(n, TURN_LOG_LIMIT_MAX))
        log_path = skysheep_home() / "logs" / "desktop.log"
        turns: list[dict] = []
        if log_path.exists():
            turns = await asyncio.to_thread(_load_turn_rows, log_path, sid)
        return {"session_id": sid, "limit": n, "turns": turns[-n:]}
