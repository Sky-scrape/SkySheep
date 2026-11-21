"""事件 kind 契约:引擎 AgentEvent ↔ 前端渲染的机械对照(派生,不手抄)。

引擎 events.py 的每个事件类用 ``kind: Literal["…"] = "…"`` 声明判别值,
前端 app*.js 靠字面字符串比较分发它们——两侧此前没有机械契约,新增事件类
漏接时只能靠人工发现。本文件双向派生:

- 引擎一侧 import skysheep.events 反射拿全部事件子类的 kind 字面值
  (单一事实来源是 events.py 本体,不手抄清单);
- 前端一侧用正则扫描 server/static/app*.js 中 kind 的字符串比较。

「kind 字符串比较」按三种形态计(全部实证存在于前端):
  1. ``case "…":`` —— switch(kind) 分支,主分发在 app.js 的 handleEvent;
  2. ``kind === "…"`` / ``kind !== "…"`` —— 含 ev.kind / data.kind 等变量;
  3. ``event === "…"`` —— 事件记录字段(如 app.js 重放补帧里的 m.event)。

断言:每个引擎事件 kind 必须要么被前端字面比较命中,要么登记进文件顶部
ALLOWLIST(逐条附写实理由)。**前端确实没处理的引擎事件是有效发现**——
新增事件类时本测试会红,逼着补前端分支,或在 ALLOWLIST 写清为什么不补;
反向(前端补上处理后 ALLOWLIST 还留着过期豁免)也会红。

已知噪声边界:case 标签按 app*.js 全量扫描,不锁定 handleEvent 函数体
(花括号配平的正则不可靠)。引擎 kind 是长 snake_case 词,与其他 switch 的
判别值重名概率可忽略;已核 error / usage / notice / compaction 等短 token
当前只出现在 handleEvent 的 case 里。
"""

from __future__ import annotations

import re
from pathlib import Path

import skysheep.events

# 引擎事件里前端确实没有字面比较处理的 kind → 写实理由。
# 「前端应该处理而漏了」的事件不许进这里——要么补前端,要么写清为什么不补;
# 前端补上处理后对应条目要删掉(test_allowlist_entries_stay_honest 会拦)。
ALLOWLIST: dict[str, str] = {
    "turn_started": (
        "前端收到但不字面比较:一次迭代开始没有独立 UI,运行态与计时由"
        " task_estimate / 首个增量(text_delta / thinking_delta /"
        " tool_call_started)驱动。真实消费者都在前端之外:CLI 渲染"
        " (cli/render.py)、后端自身(server/backend.py 的 log_session_event"
        " 以它为「当前轮」补帧缓冲的清空标记)、automation 流水线"
        " (backend_parts/automation.py)。事件本身仍随流下发(非增量帧,"
        " 经 StreamDeltaMerger 强制冲刷),前端收到即忽略。"
    ),
}

_STATIC = Path(skysheep.events.__file__).resolve().parent / "server" / "static"

# kind 比较的三种扫描形态(见模块 docstring);token 词表限小写蛇形,过滤噪声
_KIND_PAT = re.compile(
    r'case\s*"([a-z_0-9]+)"\s*:'
    r'|\bkind\s*(?:===|!==)\s*"([a-z_0-9]+)"'
    r'|\bevent\s*(?:===|!==)\s*"([a-z_0-9]+)"'
)


def _engine_event_kinds() -> dict[str, str]:
    """反射 skysheep.events:kind 字面值 → 事件类名。"""
    kinds: dict[str, str] = {}
    for name in dir(skysheep.events):
        obj = getattr(skysheep.events, name)
        if not isinstance(obj, type) or not issubclass(obj, skysheep.events.Event):
            continue
        if obj is skysheep.events.Event:
            continue  # 基类 kind 是普通 str 且无默认值,不是可分发的事件形态
        default = obj.model_fields["kind"].default
        if not isinstance(default, str):
            raise AssertionError(
                f"{obj.__name__} 的 kind 字段缺字面默认值——事件类必须声明"
                ' kind: Literal["…"] = "…",否则前端契约无法机械对照')
        assert default not in kinds, (
            f"kind={default!r} 被 {kinds[default]} 与 {obj.__name__} 重复声明,"
            "前端按 kind 分发会串")
        kinds[default] = obj.__name__
    return kinds


def _frontend_kind_strings() -> dict[str, list[str]]:
    """扫描 app*.js 里的 kind 字符串比较,返回 比较值 → 出现的文件列表。"""
    hits: dict[str, list[str]] = {}
    for path in sorted(_STATIC.glob("app*.js")):
        text = path.read_text(encoding="utf-8")
        for m in _KIND_PAT.finditer(text):
            value = next(g for g in m.groups() if g)
            hits.setdefault(value, []).append(path.name)
    return hits


def test_event_kind_contract_frontend_covers_engine_kinds():
    """每个引擎事件 kind 要么被前端字面比较命中,要么在 ALLOWLIST。"""
    kinds = _engine_event_kinds()
    # 反射必须真拿到事件(数量下限只做防呆,不锁精确值:空表会让断言空转变假绿)
    assert len(kinds) >= 30, (
        f"反射只拿到 {len(kinds)} 个事件 kind,疑似提取失效(events.py 的事件类"
        "应远多于此)")
    hits = _frontend_kind_strings()
    assert hits, "app*.js 扫描不到任何 kind 比较,疑似扫描失效"

    missing = sorted(set(kinds) - set(hits) - set(ALLOWLIST))
    assert not missing, (
        "引擎事件 kind 前端既未字面比较、也不在 ALLOWLIST"
        "(新增事件类请补 handleEvent 等前端分支,或写清理由进 ALLOWLIST):\n  "
        + "\n  ".join(f"{k}  ({kinds[k]})" for k in missing))

    stale = sorted(set(ALLOWLIST) - set(kinds))
    assert not stale, (
        "ALLOWLIST 里有引擎已不存在的 kind(事件类被删,条目请一并清理):\n  "
        + "\n  ".join(stale))


def test_allowlist_entries_stay_honest():
    """ALLOWLIST 的每条豁免都必须仍然属实:该 kind 确实没被前端字面比较。

    前端补上处理后(如给 turn_started 加了 case 分支),对应条目就成了过期
    豁免——删掉它,让契约回到「前端处理」一侧。
    """
    kinds = _engine_event_kinds()
    unknown = sorted(set(ALLOWLIST) - set(kinds))
    assert not unknown, f"ALLOWLIST 含未知 kind(不在 events.py): {unknown}"
    hits = _frontend_kind_strings()
    lying = sorted(set(ALLOWLIST) & set(hits))
    assert not lying, (
        "ALLOWLIST 条目已过期——这些 kind 前端现在有字面比较,豁免理由不再"
        "成立,请从 ALLOWLIST 删除:\n  "
        + "\n  ".join(f"{k}  出现于 {', '.join(hits[k])}" for k in lying))
