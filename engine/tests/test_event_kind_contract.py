"""事件 kind 契约:引擎 AgentEvent ↔ 前端 AP_EVENTS 常量表 ↔ 渲染分发的机械对照(派生,不手抄)。

引擎 events.py 的每个事件类用 ``kind: Literal["…"] = "…"`` 声明判别值,
前端 app*.js 靠字符串比较分发它们。迁移前是比较裸字面量、两侧没有机械契约;
现在 kind 值由 engine/tools/gen_frontend_events.py 从 events.py 反射生成
``static/app-events.js`` 的 ``AP_EVENTS`` 冻结常量表(单一事实来源是
events.py 本体),app.js 的比较一律查表引用。本文件四条用例锁死这条链:

1. ``test_event_kind_contract_frontend_covers_engine_kinds`` —— 每个引擎 kind
   要么被前端比较命中,要么登记进 ALLOWLIST(逐条附写实理由)。命中按「字面
   三形态 ∪ AP_EVENTS 引用三形态」计,两态之间没有灰色地带;表里没有的
   ``AP_EVENTS.X`` 引用(拼错常量名)直接红;
2. ``test_allowlist_entries_stay_honest`` —— ALLOWLIST 的每条豁免必须仍然属实
   (该 kind 依旧没有任何比较,字面或引用都算);
3. ``test_app_events_file_matches_engine`` —— 盘上 app-events.js 与 events.py
   反射双射,且与生成器现算内容逐字一致(EOL 归一化):生成过期 / 手改即红;
4. ``test_no_raw_engine_kind_literals_in_frontend`` —— 残余扫描:引擎 kind 在
   前端不得再以裸字面量比较出现(谁写回谁红),与用例 1 合成迁移面收敛钳。

「kind 字符串比较」按三种形态计(全部实证存在于前端):
  1. ``case "…":`` / ``case AP_EVENTS.X:`` —— switch(kind) 分支,主分发在
     app.js 的 handleEvent;
  2. ``kind === "…"`` / ``kind !== "…"``(含 AP_EVENTS 引用形态)—— 含
     ev.kind / data.kind 等变量;
  3. ``event === "…"`` —— 事件记录字段(如 app.js 重放补帧里的 m.event)。

已知噪声边界:case 标签按 app*.js 全量扫描,不锁定 handleEvent 函数体
(花括号配平的正则不可靠)。引擎 kind 是长 snake_case 词,与其他 switch 的
判别值重名概率可忽略;已核 error / usage / notice / compaction 等短 token
当前只出现在 handleEvent 的 case 里。后端 WS 广播的非引擎 kind
(update_progress / relay_pair_* / session_updated / cron_updated 等)不经
events.py 事件类,不进 AP_EVENTS 表,前端分支仍是字面量——残余扫描只对
引擎 kind 生效,不误伤它们。广播 kind 出仓已评估为不做:服务端 ``"kind": "…"``
字面量里混着嵌套载荷字段(task / phase / pair 等),没有统一的 emit 注册表,
按字面量派生会同时误收与漏收,两处真源反而更难守;改名风险已由两侧行为测试
(更新进度、relay_pair_*、term_*、session_updated 等)覆盖。
"""

from __future__ import annotations

import importlib.util
import re
from difflib import unified_diff
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
_TABLE_PATH = _STATIC / "app-events.js"

# —— 生成器加载:tools/ 无 __init__.py,不走 package import,
#    importlib 按路径加载(沿用 test_ws_reference_doc.py 的先例)。——
_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location(
    "gen_frontend_events", _TOOLS / "gen_frontend_events.py")
assert _SPEC and _SPEC.loader
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)

_REGEN_HINT = "重跑 uv run python tools/gen_frontend_events.py 更新产物"

# kind 比较的字面三形态(旧口径,保留:任何文件将来新增裸字面量仍被计入命中,
# 并由用例 4 单独拦);token 词表限小写蛇形,过滤噪声
_KIND_PAT = re.compile(
    r'case\s*"([a-z_0-9]+)"\s*:'
    r'|\bkind\s*(?:===|!==)\s*"([a-z_0-9]+)"'
    r'|\bevent\s*(?:===|!==)\s*"([a-z_0-9]+)"'
)

# AP_EVENTS 引用的三形态(迁移后口径);名字捕获后经盘上表反解为 kind
_AP_REF_PAT = re.compile(
    r'case\s+AP_EVENTS\.([A-Z_0-9]+)\s*:'
    r'|\bkind\s*(?:===|!==)\s*AP_EVENTS\.([A-Z_0-9]+)'
    r'|\bevent\s*(?:===|!==)\s*AP_EVENTS\.([A-Z_0-9]+)'
)

# 任意上下文的 AP_EVENTS.NAME 引用(未知名字检查比三种比较形态更宽:
# 拼错常量名无论写在哪种表达式里都逃不掉)
_ANY_AP_REF_PAT = re.compile(r"\bAP_EVENTS\.([A-Z_0-9]+)")


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


def _table() -> dict[str, str]:
    """盘上 app-events.js 的 NAME → kind(parse_table 单一实现)。"""
    if not _TABLE_PATH.exists():
        raise AssertionError(f"{_TABLE_PATH} 不存在。{_REGEN_HINT}")
    return gen.parse_table(_TABLE_PATH.read_text(encoding="utf-8"))


def _frontend_kind_hits() -> dict[str, list[str]]:
    """扫描 app*.js 里的 kind 比较(字面三形态 ∪ AP_EVENTS 引用三形态),
    返回 比较值(kind) → 出现的文件列表。

    AP_EVENTS 引用经盘上表反解为 kind;表里没有的名字在这里先不计入,
    由 covers 用例的未知引用断言统一拦(失败信息里点名)。
    """
    table = _table()
    hits: dict[str, list[str]] = {}
    for path in sorted(_STATIC.glob("app*.js")):
        text = path.read_text(encoding="utf-8")
        for m in _KIND_PAT.finditer(text):
            value = next(g for g in m.groups() if g)
            hits.setdefault(value, []).append(path.name)
        for m in _AP_REF_PAT.finditer(text):
            name = next(g for g in m.groups() if g)
            kind = table.get(name)
            if kind is not None:
                hits.setdefault(kind, []).append(path.name)
    return hits


def _unknown_ap_refs() -> dict[str, list[str]]:
    """app*.js 里表外名字的 AP_EVENTS.X 引用(拼错常量名)。"""
    table = _table()
    unknown: dict[str, list[str]] = {}
    for path in sorted(_STATIC.glob("app*.js")):
        text = path.read_text(encoding="utf-8")
        for m in _ANY_AP_REF_PAT.finditer(text):
            name = m.group(1)
            if name not in table:
                unknown.setdefault(name, []).append(path.name)
    return unknown


def test_event_kind_contract_frontend_covers_engine_kinds():
    """每个引擎事件 kind 要么被前端比较命中(字面或 AP_EVENTS 引用),要么在 ALLOWLIST。"""
    kinds = _engine_event_kinds()
    # 反射必须真拿到事件(数量下限只做防呆,不锁精确值:空表会让断言空转变假绿)
    assert len(kinds) >= 30, (
        f"反射只拿到 {len(kinds)} 个事件 kind,疑似提取失效(events.py 的事件类"
        "应远多于此)")
    hits = _frontend_kind_hits()
    assert hits, "app*.js 扫描不到任何 kind 比较,疑似扫描失效"

    unknown = _unknown_ap_refs()
    assert not unknown, (
        "前端引用了 AP_EVENTS 表里不存在的常量名(拼错名字查表会拿到 undefined,"
        "比较恒假、分支静默失效):\n  "
        + "\n  ".join(f"{n}  出现于 {', '.join(files)}" for n, files in sorted(unknown.items())))

    missing = sorted(set(kinds) - set(hits) - set(ALLOWLIST))
    assert not missing, (
        "引擎事件 kind 前端既未比较(字面或 AP_EVENTS 引用)、也不在 ALLOWLIST"
        "(新增事件类请补 handleEvent 等前端分支,或写清理由进 ALLOWLIST):\n  "
        + "\n  ".join(f"{k}  ({kinds[k]})" for k in missing))

    stale = sorted(set(ALLOWLIST) - set(kinds))
    assert not stale, (
        "ALLOWLIST 里有引擎已不存在的 kind(事件类被删,条目请一并清理):\n  "
        + "\n  ".join(stale))


def test_allowlist_entries_stay_honest():
    """ALLOWLIST 的每条豁免都必须仍然属实:该 kind 确实没被前端比较。

    前端补上处理后(如给 turn_started 加了 case 分支,字面或 AP_EVENTS 引用
    都算),对应条目就成了过期豁免——删掉它,让契约回到「前端处理」一侧。
    """
    kinds = _engine_event_kinds()
    unknown = sorted(set(ALLOWLIST) - set(kinds))
    assert not unknown, f"ALLOWLIST 含未知 kind(不在 events.py): {unknown}"
    hits = _frontend_kind_hits()
    lying = sorted(set(ALLOWLIST) & set(hits))
    assert not lying, (
        "ALLOWLIST 条目已过期——这些 kind 前端现在有比较(字面或 AP_EVENTS 引用),"
        "豁免理由不再成立,请从 ALLOWLIST 删除:\n  "
        + "\n  ".join(f"{k}  出现于 {', '.join(hits[k])}" for k in lying))


def test_app_events_file_matches_engine():
    """盘上 app-events.js 必须与 events.py 反射双射,并与生成器现算逐字一致。

    双射:缺一个、多一个都红(失败信息点名 kind 与事件类名);
    命名规则:NAME == kind.upper()(parse_table 反解的依据,锁死);
    新鲜度:EOL 归一化后与 build() 现算逐字一致——生成过期或手改即红,
    失败信息带 unified diff + 重跑命令提示(与 test_ws_reference_doc 同款体验)。
    """
    kinds = _engine_event_kinds()
    table = _table()

    missing = sorted(set(kinds) - set(table.values()))
    extra = sorted(set(table.values()) - set(kinds))
    assert not missing and not extra, (
        "app-events.js 与引擎事件类不是双射:\n"
        + "".join(f"  缺 {k}  ({kinds[k]})\n" for k in missing)
        + "".join(f"  多 {k}(不在 events.py)\n" for k in extra)
        + f"  {_REGEN_HINT}")

    bad_names = {n: k for n, k in table.items() if n != k.upper()}
    assert not bad_names, (
        "AP_EVENTS 命名规则被破坏(NAME != kind.upper(),parse_table 反解失效):\n  "
        + "\n  ".join(f"{n} -> {k}" for n, k in sorted(bad_names.items())))

    on_disk = _TABLE_PATH.read_text(encoding="utf-8")
    built = gen.build()
    norm = gen._normalize_eol  # 生成器同款 EOL 归一化，单一实现
    if norm(on_disk) != norm(built):
        diff = "\n".join(list(unified_diff(
            on_disk.splitlines(),
            built.splitlines(),
            fromfile="static/app-events.js（盘上）",
            tofile="生成器现算（重新生成）",
            lineterm="",
        ))[:60])
        raise AssertionError(
            "static/app-events.js 与生成器现算内容不一致（生成产物过期或被手改）。\n"
            f"{_REGEN_HINT}\n\n{diff}")


def test_no_raw_engine_kind_literals_in_frontend():
    """app*.js 里引擎 kind 不得再以裸字面量比较出现——一律经 AP_EVENTS 引用。

    与用例 1 合成迁移面收敛钳:字面量被禁(本用例)、常量引用被认(用例 1),
    两态之间没有灰色地带;谁写回裸字面量谁红,新增事件也不会被静默漏接。

    误伤边界(2026-11-22 迁移时逐处核实):引擎 kind token 在三种比较形态下
    只出现在事件分发处(handleEvent / flushReplayBuf / resumeEventStreams /
    renderSubEvent / subHudEvent,共 48 处),11 处同名巧合 token(CSS 类
    notice、status === "error"、DOM id usage、工单操作键等)不匹配比较形态、
    不在本扫描面内。将来若某非事件 switch 碰巧用了 case "error": 这类形态,
    修法是换判别变量名(不叫 kind/event)——优先修命名,不给本测试开豁免
    清单(豁免清单会重演 ALLOWLIST 腐化)。
    """
    kinds = _engine_event_kinds()
    assert len(kinds) >= 30, (
        f"反射只拿到 {len(kinds)} 个事件 kind,疑似提取失效(events.py 的事件类"
        "应远多于此)")
    raw: dict[str, list[str]] = {}
    for path in sorted(_STATIC.glob("app*.js")):
        text = path.read_text(encoding="utf-8")
        for m in _KIND_PAT.finditer(text):
            value = next(g for g in m.groups() if g)
            if value in kinds:
                raw.setdefault(value, []).append(path.name)
    assert not raw, (
        "引擎事件 kind 又以裸字面量出现在前端比较里(必须改用 AP_EVENTS.X 引用,"
        "否则生成表与比较点会漂移):\n  "
        + "\n  ".join(f"{k}  出现于 {', '.join(files)}" for k, files in sorted(raw.items())))
