"""Mods 扩展系统（实验性）：第三方 JS 事件处理函数，挂进引擎事件流。

对标 Claude Code Mods 的「JS 事件处理扩展」形态，按设计文档（.zcode/plans/plan-mods.md）
实现 SkySheep 子集。核心安全立场——**Mod 对权限门只能收紧（拒绝/要求确认），绝不放行**：

- **JS handler 全在权限门之后**：permissionRequest / toolPre / toolPost / iterationStart /
  turnStop 五个挂点全部位于 authorize() 完成之后；门之前/门内的 Mod 面只有 declarative
  静态表（纯 Python 求值）：``deny_tools`` 在 authorize 之前命中即拒绝（少执行），
  ``require_confirm_tools`` 经 PermissionGate.extra_confirm 只会把「自动放行」降级为
  「逐次确认」——该接口没有「返回 False 来放行」的语义，False/None/异常一律「不干预」。
- **动作闭集、无 allow 无 modify**：handler 返回经 :func:`_parse_action_result` 白名单
  解析，合法键只有 {deny, note, ui}；出现 allow / modifyInput 等任何闭集外键一律丢弃
  并计错误（fail-closed，与 gate.normalize_decision 同姿态）。
- **沙箱无宿主注入**：quickjs 解释器里不注册任何文件/网络/进程 API——Mod 默认没有
  任何任意 IO 能力；唯一注入面是 ``sky.state``（本 Mod、本会话键的持久 JSON，由引擎
  经 textio.write_text_atomic 代存）与 ``sky.now()``。
- **per-Mod 单线程执行**：quickjs runtime 线程不安全，每个 Mod 一个
  ThreadPoolExecutor(max_workers=1)，Agent 侧经 run_in_executor 派发——同一 runtime
  永远在同一线程上执行、调用天然串行。超时用 ``set_time_limit``（quickjs-ng 绑定的
  每 次执行看门狗；设计文档写的是 set_interrupt_handler，本绑定实际暴露的等价接口是
  set_time_limit，能力语义一致）。

依赖探测失败（未装 quickjs / 能力不齐）时 ModManager 进入「禁用态」：清单照常展示，
declarative 收紧声明照常生效（纯 Python），JS handler 不执行。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ..config import skysheep_home
from ..textio import read_text_file, write_text_atomic

# ---- 契约常量 ----

# 清单文件名：mod.json（SkySheep）；mods.json 是 Claude Code Mods 的别名（两都认）
MANIFEST_NAMES = ("mod.json", "mods.json")
README_NAME = "README.md"

# manifest 字段白名单：未知字段拒装（fail-closed）
MANIFEST_FIELDS = frozenset({
    "id", "name", "version", "description", "author", "entry",
    "api", "hooks", "permissions", "declarative", "cc_mods",
})

# CC Mods 兼容桥接：具名导出 / hooks 对象成员 → 内部 hook
CC_NAMED_EXPORT_BRIDGE = {
    "onToolCall": "tool_pre",
    "onToolResult": "tool_post",
    "onPermissionRequest": "permission_request",
    "promptSubmit": "iteration_start",
    "onStop": "turn_stop",
}

# hook 名（manifest 用内部名；main.js 里用 camelCase 方法名）
MOD_HOOKS = ("tool_pre", "tool_post", "permission_request", "iteration_start", "turn_stop")
HOOK_TO_JS = {
    "tool_pre": "toolPre",
    "tool_post": "toolPost",
    "permission_request": "permissionRequest",
    "iteration_start": "iterationStart",
    "turn_stop": "turnStop",
}

# 每个 hook 的 JS 候选方法名（按序尝试）：SkySheep camelCase 名在前，CC 风格
# 具名导出（onToolCall / onPermissionRequest / promptSubmit…）在后——探测与
# 调用共用同一张表，CC Mods 的入口形状由此桥接为内部 handler。
HOOK_JS_NAMES: dict[str, list[str]] = {k: [v] for k, v in HOOK_TO_JS.items()}
for _cc_name, _internal in CC_NAMED_EXPORT_BRIDGE.items():
    if _cc_name not in HOOK_JS_NAMES[_internal]:
        HOOK_JS_NAMES[_internal].append(_cc_name)

# permissions 两档：observe（handler 只能 note/ui）与 tighten（还可 deny + declarative）
MOD_PERMISSIONS = ("observe", "tighten")
# CC 清单里更宽的 permissions 档位：一律收敛为 tighten 并提示，绝不映射为放行
CC_WIDER_PERMISSIONS = frozenset({
    "allow", "write", "read", "all", "full", "full_access", "network", "fs", "shell", "execute",
})

# declarative 收紧表：只有 tighten 档允许声明
DECLARATIVE_FIELDS = frozenset({"deny_tools", "require_confirm_tools"})

# 沙箱限制
MAX_ENTRY_BYTES = 128 * 1024
HANDLER_TIME_LIMIT_S = 0.2          # 单次 handler 执行硬顶（每次执行独立计时）
HANDLER_HARD_DEADLINE_S = 1.0       # 绑定层僵死的外层保险（正常时 set_time_limit 先到）
MEMORY_LIMIT_BYTES = 16 * 1024 * 1024
MAX_CONSECUTIVE_TIMEOUTS = 3        # 连续超时 → 进程内自动停用（不写回 config，重启复活）

# 文本限长（skills/loader 同口径的压缩空白）
MAX_ID_CHARS = 64
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 1000
MAX_VERSION_CHARS = 32
MAX_AUTHOR_CHARS = 120
MAX_MOD_NOTE_CHARS = 200
MAX_WIDGET_TEXT_CHARS = 500
MAX_WIDGETS_PER_RESULT = 8
MAX_TIMELINE_ITEMS = 20
README_PREVIEW_CHARS = 5000
MAX_STATE_BYTES = 64 * 1024
STATE_SESSION_LRU = 20

# handler 返回的动作闭集：只有这三个键（外加「无返回 = continue」）
_ACTION_KEYS = frozenset({"deny", "note", "ui"})

# Widget 词表：kind → 允许的 slot 集合（前端固定渲染器只认这五种）
WIDGET_SLOTS = {
    "stat": {"tray"},
    "badge": {"stream", "perm"},
    "progress": {"stream"},
    "timeline": {"stream"},
    "text": {"perm"},
}
WIDGET_LEVELS = frozenset({"ok", "warn", "storm"})

MAX_RECENT_RUNS = 24                # 每 Mod 最近执行记录（环形）
MAX_MANIFEST_BYTES = 64 * 1024

# 「最近执行」记录的输出摘要截断
_SUMMARY_CHARS = 200


class ModError(Exception):
    """Mod 安装/清单错误（面向用户的中文提示）。"""


def _sanitize_text(value: object, limit: int) -> str:
    """展示文本：压空白 + 限长（skills/loader 的 name/description 同口径）。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _validate_mod_id(mod_id: str) -> str:
    """校验 Mod id 能安全当作 mods 根目录下的一个子目录名（skills._validate_skill_name 同款）。

    拒空、超长、路径分隔符、冒号（Windows 盘符 / NTFS 数据流）、纯点目录引用、
    保留名（state / 点开头）。
    """
    raw = (mod_id or "").strip()
    if not raw:
        raise ModError("Mod id 不能为空")
    if len(raw) > MAX_ID_CHARS:
        raise ModError(f"Mod id 过长（最多 {MAX_ID_CHARS} 字符）：{raw[:80]}…")
    if "/" in raw or "\\" in raw:
        raise ModError(f"Mod id 不能包含路径分隔符（/ 或 \\）：{raw}")
    if ":" in raw:
        raise ModError(f"Mod id 不能包含冒号（盘符/数据流）：{raw}")
    if raw.strip(". ") == "":
        raise ModError(f"Mod id 不能是目录引用：{raw}")
    if raw.casefold() == "state" or raw.startswith("."):
        # 保留名：state 是全部 Mod 持久状态的存放目录（mods_state_root），装成
        # Mod 会在覆盖安装时把状态目录整个换掉；点开头目录 load_all 永远跳过
        # （装了也是永不加载的僵尸目录）。Windows 文件系统大小写不敏感，按
        # casefold 比对 state。一律拒，提示改 id。
        raise ModError(f"Mod id 是保留名，请换一个 id（不能用 state 或点开头）：{raw}")
    return raw


def _mod_target(root: Path, mod_id: str) -> Path:
    """算出并校验安装/删除落点（skills._skill_target 同款两道关卡）。"""
    clean = _validate_mod_id(mod_id)
    root_path = Path(root).expanduser()
    root_resolved = root_path.resolve()
    target = (root_path / clean).resolve()
    if target == root_resolved or not target.is_relative_to(root_resolved):
        raise ModError(f"Mod id 指向 Mods 目录之外，已拒绝：{clean}")
    return target


def mods_root() -> Path:
    """Mod 安装根目录：~/.skysheep/mods/<id>/（state 子目录同根）。"""
    return skysheep_home() / "mods"


def mods_state_root() -> Path:
    return mods_root() / "state"


def parse_manifest(data: object, *, source_label: str = "") -> dict:
    """校验并归一 mod.json（未知字段拒装；CC 宽档收敛；入口落点校验在安装器做）。

    返回归一化清单 dict；不合法抛 ModError（中文提示）。
    """
    if not isinstance(data, dict):
        raise ModError(f"清单必须是 JSON 对象：{source_label}")
    unknown = sorted(set(data) - MANIFEST_FIELDS)
    if unknown:
        raise ModError(
            f"清单含未知字段，已拒绝（实验性兼容面只认 {', '.join(sorted(MANIFEST_FIELDS))}）："
            + "、".join(unknown)
        )
    mod_id = _validate_mod_id(str(data.get("id", "")))
    name = _sanitize_text(data.get("name") or mod_id, MAX_NAME_CHARS) or mod_id
    version = _sanitize_text(data.get("version") or "0.0.0", MAX_VERSION_CHARS)
    description = _sanitize_text(data.get("description"), MAX_DESCRIPTION_CHARS)
    author = _sanitize_text(data.get("author"), MAX_AUTHOR_CHARS)
    entry = str(data.get("entry") or "main.js").strip() or "main.js"

    raw_api = data.get("api", 1)
    cc = data.get("cc_mods") if isinstance(data.get("cc_mods"), dict) else {}
    notes: list[str] = []
    if raw_api is None or raw_api == "":
        raw_api = 1
    if not isinstance(raw_api, int) or isinstance(raw_api, bool) or raw_api != 1:
        raise ModError(f"清单 api 只支持 1（当前值：{raw_api!r}）")

    hooks_raw = data.get("hooks") or []
    if not isinstance(hooks_raw, list):
        raise ModError("清单 hooks 必须是数组")
    hooks: list[str] = []
    for h in hooks_raw:
        hs = str(h).strip()
        if hs not in MOD_HOOKS:
            raise ModError(f"清单 hooks 含未知事件：{hs}（支持 {' / '.join(MOD_HOOKS)}）")
        if hs not in hooks:
            hooks.append(hs)

    perm_raw = str(data.get("permissions") or "").strip()
    if not perm_raw:
        # CC 清单缺 permissions 按 tighten 收敛（更保守的一档）
        perm_raw = "tighten"
        if data.get("permissions") is not None or cc.get("compatible"):
            notes.append("清单未声明 permissions，按 tighten（收紧档）处理")
    if perm_raw in MOD_PERMISSIONS:
        permissions = perm_raw
    elif perm_raw.lower() in CC_WIDER_PERMISSIONS:
        # CC 更宽档位：收敛为 tighten 并在清单回显，绝不映射为放行
        permissions = "tighten"
        notes.append(f"CC permissions「{perm_raw}」宽于 SkySheep 的两档，已收敛为 tighten（实验性兼容）")
    else:
        raise ModError(f"清单 permissions 只支持 observe / tighten：{perm_raw}")

    declarative_raw = data.get("declarative")
    declarative: dict[str, list[str]] = {"deny_tools": [], "require_confirm_tools": []}
    if declarative_raw is not None:
        if not isinstance(declarative_raw, dict):
            raise ModError("清单 declarative 必须是对象")
        unknown_decl = sorted(set(declarative_raw) - DECLARATIVE_FIELDS)
        if unknown_decl:
            raise ModError("清单 declarative 含未知字段：" + "、".join(unknown_decl))
        if permissions != "tighten":
            raise ModError("declarative 收紧声明只有 tighten 档的 Mod 允许声明")
        for key in DECLARATIVE_FIELDS:
            items = declarative_raw.get(key) or []
            if not isinstance(items, list):
                raise ModError(f"declarative.{key} 必须是字符串数组")
            cleaned: list[str] = []
            for item in items:
                pat = str(item or "").strip()
                if pat and pat not in cleaned:
                    cleaned.append(pat[:120])
            declarative[key] = cleaned

    cc_mods: dict[str, object] = {}
    if cc:
        cc_mods = {
            "compatible": bool(cc.get("compatible", False)),
            "upstream": _sanitize_text(cc.get("upstream"), 300),
        }

    return {
        "id": mod_id,
        "name": name,
        "version": version,
        "description": description,
        "author": author,
        "entry": entry,
        "api": 1,
        "hooks": hooks,
        "permissions": permissions,
        "declarative": declarative,
        "cc_mods": cc_mods,
        "notes": notes,
    }


def load_manifest_from_dir(mod_dir: Path) -> tuple[dict, Path]:
    """从 Mod 目录读清单：mod.json / mods.json（CC 别名）任一存在即可。"""
    manifest_path: Path | None = None
    for name in MANIFEST_NAMES:
        candidate = mod_dir / name
        try:
            if candidate.is_file():
                manifest_path = candidate
                break
        except OSError:
            continue
    if manifest_path is None:
        raise ModError(f"目录里没有 mod.json（或 CC 别名 mods.json）：{mod_dir.name}")
    try:
        tf = read_text_file(manifest_path)
    except OSError as e:
        raise ModError(f"读清单失败：{e}") from e
    if tf.binary:
        raise ModError("清单不是文本文件")
    if len(tf.text) > MAX_MANIFEST_BYTES:
        raise ModError("清单过大")
    try:
        data = json.loads(tf.text)
    except ValueError as e:
        raise ModError(f"清单不是有效 JSON：{e}") from e
    return parse_manifest(data, source_label=str(manifest_path)), manifest_path


def _normalize_widgets(raw: object) -> list[dict]:
    """Widget 词表白名单：未知 kind/越界 slot 丢弃，文本限长（前端固定渲染器的契约）。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:MAX_WIDGETS_PER_RESULT]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        slot = str(item.get("slot", "")).strip()
        if kind not in WIDGET_SLOTS or slot not in WIDGET_SLOTS[kind]:
            continue
        widget: dict[str, Any] = {"kind": kind, "slot": slot}
        if kind == "timeline":
            items_raw = item.get("items") or []
            items: list[dict] = []
            if isinstance(items_raw, list):
                for it in items_raw[:MAX_TIMELINE_ITEMS]:
                    if not isinstance(it, dict):
                        continue
                    items.append({
                        "t": _sanitize_text(it.get("t"), 40),
                        "text": _sanitize_text(it.get("text"), MAX_WIDGET_TEXT_CHARS),
                    })
            if not items:
                continue
            widget["items"] = items
        else:
            if kind == "progress":
                try:
                    widget["value"] = max(0, min(100, float(item.get("value") or 0)))
                except (TypeError, ValueError):
                    continue
            if kind == "stat":
                level = str(item.get("level", "ok")).strip()
                widget["level"] = level if level in WIDGET_LEVELS else "ok"
            widget["text"] = _sanitize_text(item.get("text"), MAX_WIDGET_TEXT_CHARS)
        out.append(widget)
    return out


def _parse_action_result(
    raw: object, *, hook: str, allow_deny: bool, mod_id: str, errors: list[str],
) -> dict:
    """handler 返回白名单解析：合法键只有 {deny, note, ui}（闭集外一律丢弃并计错误）。

    deny 只有 tighten 档的 Mod 在允许 deny 的 hook 上有效；observe 档的 deny 一律丢弃。
    返回 {"deny": str|None, "note": str, "ui": list}。
    """
    result: dict[str, Any] = {"deny": None, "note": "", "ui": []}
    if raw is None:
        return result
    if not isinstance(raw, dict):
        errors.append(f"{mod_id}: {hook} 返回必须是对象（收到 {type(raw).__name__}）")
        return result
    # 闭集外动作键（modifyInput / allow / decision…）一律丢弃并计错误——这是
    # 「Mod 永远没有 modify / allow 能力」的结构保证之一
    for key in raw:
        if key not in _ACTION_KEYS:
            errors.append(f"{mod_id}: {hook} 闭集外的动作键「{key}」已丢弃（Mod 没有 {key} 能力）")
    if raw.get("deny") is not None:
        if allow_deny:
            reason = _sanitize_text(raw.get("deny"), MAX_MOD_NOTE_CHARS)
            if reason:
                result["deny"] = reason
            else:
                errors.append(f"{mod_id}: {hook} deny 原因为空，已丢弃")
        else:
            errors.append(f"{mod_id}: {hook} observe 档不允许 deny，已丢弃")
    if raw.get("note") is not None:
        note = _sanitize_text(raw.get("note"), MAX_MOD_NOTE_CHARS)
        if note:
            result["note"] = note
    result["ui"] = _normalize_widgets(raw.get("ui"))
    return result


# ---- JS 入口装配 ----
#
# quickjs 绑定的 eval() 只跑 script、ctx.module() 的命名空间对象穿不回 Python 侧
# 属性访问（实测 0.15.1.1），所以入口装配用「源码变换 + IIFE」：把 `export default`
# 与 CC 风格具名导出收进 __sky_default__ / __sky_named__ 两张表，再把
# {d, n} 对象挂到 globalThis 上供 Python 侧取回。单文件 ≤128KB 的静态审查面，
# 透明、可单测。

_EXPORT_DEFAULT_RE = re.compile(r"\bexport\s+default\b")
_EXPORT_FUNC_RE = re.compile(r"\bexport\s+(async\s+)?function\s+([A-Za-z_$][\w$]*)")
_EXPORT_VAR_RE = re.compile(r"\bexport\s+(const|let|var)\s+([A-Za-z_$][\w$]*)")
_EXPORT_BRACE_RE = re.compile(r"\bexport\s*\{([^}]*)\}")
_IMPORT_RE = re.compile(r"^\s*import\s", re.MULTILINE)

# 沙箱引导（每次 Context 创建时 eval 一次）：__sky_call__ 负责取出 handler、
# 注入 sky.state / sky.now、执行并回传状态；__sky_has__ 只探测 handler 是否存在。
_SANDBOX_BOOTSTRAP = """
function __sky_fn__(mod, nm) {
  if (!mod) return undefined;
  var d = (mod.d !== undefined && mod.d !== null) ? mod.d : null;
  if (d && typeof d === 'object') {
    if (typeof d[nm] === 'function') return d[nm];
    if (d.hooks && typeof d.hooks === 'object' && typeof d.hooks[nm] === 'function')
      return d.hooks[nm];
  }
  if (mod.n && typeof mod.n === 'object') {
    if (typeof mod.n[nm] === 'function') return mod.n[nm];
    if (mod.n.hooks && typeof mod.n.hooks === 'object'
        && typeof mod.n.hooks[nm] === 'function') return mod.n.hooks[nm];
  }
  return undefined;
}
globalThis.__sky_call__ = function(mod, namesJson, payloadJson, stateJson) {
  var names = JSON.parse(namesJson);
  var f;
  for (var i = 0; i < names.length && f === undefined; i++) f = __sky_fn__(mod, names[i]);
  if (typeof f !== 'function') return undefined;
  var p = (payloadJson === null || payloadJson === undefined || payloadJson === '')
    ? {} : JSON.parse(payloadJson);
  globalThis.sky = {
    state: JSON.parse(stateJson || 'null') || {},
    now: function() { return Date.now(); },
  };
  var r = f(p);
  try { globalThis.__sky_state_out__ = JSON.stringify(globalThis.sky.state); }
  catch (e) { globalThis.__sky_state_out__ = '{}'; }
  return r;
};
globalThis.__sky_detect__ = function(mod, specJson) {
  var spec = JSON.parse(specJson);
  var out = {};
  for (var k in spec) {
    var found = false;
    for (var i = 0; i < spec[k].length && !found; i++) {
      if (__sky_fn__(mod, spec[k][i]) !== undefined) found = true;
    }
    out[k] = found;
  }
  return JSON.stringify(out);
};
"""


def assemble_entry_source(source: str) -> str:
    """把 main.js 源码装配成 IIFE：收集 default 导出与具名导出到 __skymod__。"""
    if _IMPORT_RE.search(source):
        raise ModError("main.js 不支持 import 语句（沙箱没有模块加载器，也不提供任何外部依赖）")
    js = _EXPORT_DEFAULT_RE.sub("__sky_default__ =", source, count=1)
    named: list[str] = []

    def _func_sub(m: re.Match) -> str:
        name = m.group(2)
        named.append(name)
        return f'__sky_named__["{name}"] = {m.group(1) or ""}function {name}'

    js = _EXPORT_FUNC_RE.sub(_func_sub, js)

    def _var_sub(m: re.Match) -> str:
        named.append(m.group(2))
        return f"{m.group(1)} {m.group(2)}"

    js = _EXPORT_VAR_RE.sub(_var_sub, js)

    def _brace_sub(m: re.Match) -> str:
        for piece in m.group(1).split(","):
            piece = piece.strip()
            if not piece:
                continue
            local, _, alias = piece.partition(" as ")
            local = local.strip()
            alias = (alias or local).strip()
            if local:
                named.append(alias)
                js_collector = f'__sky_named__["{alias}"] = {local};'
                # 收集语句拼进被替换的位置不安全（表达式上下文），统一进尾部
                _brace_sub.collectors.append(js_collector)
        return ";"

    _brace_sub.collectors = []  # type: ignore[attr-defined]
    js = _EXPORT_BRACE_RE.sub(_brace_sub, js)

    collectors = "".join(
        f'try{{__sky_named__["{n}"]={n};}}catch(e){{}}' for n in dict.fromkeys(named)
    ) + "".join(_brace_sub.collectors)  # type: ignore[attr-defined]
    return (
        "globalThis.__skymod__ = (function(){ "
        "var __sky_default__; var __sky_named__ = {}; "
        + js + "; "
        + collectors +
        " return { d: __sky_default__, n: __sky_named__ }; })();"
    )


class _ModRuntime:
    """单个 Mod 的 quickjs 沙箱宿主。

    线程模型：per-Mod ThreadPoolExecutor(max_workers=1)，Context 的创建与所有
    eval/调用都发生在该线程上（串行）；超时由 quickjs 的每次执行看门狗
    （set_time_limit）打断，被打断后的 Context 仍可用（实测）。
    """

    def __init__(self, mod_id: str, entry_path: Path) -> None:
        self.mod_id = mod_id
        self.entry_path = entry_path
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"skymod-{mod_id}"
        )
        self._ctx: Any = None
        self._mod_obj: Any = None
        self._caller: Any = None
        self._has: Any = None

    # ---- executor 线程内执行的部分 ----

    def _sync_setup(self) -> set[str]:
        """创建 Context 并装配入口，返回实际实现 handler 的内部 hook 名集合。"""
        import quickjs

        source = self.entry_path.read_bytes()
        if len(source) > MAX_ENTRY_BYTES:
            raise ModError(
                f"main.js 超过 {MAX_ENTRY_BYTES // 1024}KB 上限（{len(source) // 1024}KB）"
            )
        text = source.decode("utf-8")
        wrapped = assemble_entry_source(text)
        ctx = quickjs.Context()
        ctx.set_memory_limit(MEMORY_LIMIT_BYTES)
        ctx.set_time_limit(HANDLER_TIME_LIMIT_S)
        ctx.eval(_SANDBOX_BOOTSTRAP)
        ctx.eval(wrapped)
        self._ctx = ctx
        self._mod_obj = ctx.get("__skymod__")
        self._caller = ctx.get("__sky_call__")
        spec = json.dumps(HOOK_JS_NAMES, ensure_ascii=False)
        try:
            detected_raw = ctx.get("__sky_detect__")(self._mod_obj, spec)
            detected = {k for k, v in json.loads(detected_raw).items() if v}
        except Exception:  # noqa: BLE001 - 探测失败按全未实现（装载面仍可用）
            detected = set()
        return detected

    def _sync_call(self, hook: str, payload: dict, state: dict) -> tuple[object, dict]:
        """执行一次 handler，返回 (原始返回值, 新状态)。在 executor 线程上运行。"""
        ctx = self._ctx
        if ctx is None:
            raise ModError("runtime 未初始化")
        ctx.set_time_limit(HANDLER_TIME_LIMIT_S)  # 每次执行独立计时
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        state_json = json.dumps(state, ensure_ascii=False, default=str)
        names_json = json.dumps(HOOK_JS_NAMES[hook], ensure_ascii=False)
        raw = self._caller(self._mod_obj, names_json, payload_json, state_json)
        if isinstance(raw, quickjs_object_type()):
            raw = json.loads(raw.json())
        state_out = ctx.get("__sky_state_out__")
        new_state: dict = {}
        if isinstance(state_out, str) and state_out:
            try:
                loaded = json.loads(state_out)
                if isinstance(loaded, dict):
                    new_state = loaded
            except ValueError:
                new_state = {}
        return raw, new_state

    # ---- 事件循环侧入口 ----

    async def setup(self) -> set[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._sync_setup)

    async def call(self, hook: str, payload: dict, state: dict) -> tuple[object, dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._sync_call, hook, payload, state
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._ctx = None
        self._mod_obj = None


def quickjs_object_type() -> type:
    """取 _quickjs.Object 类型（供 isinstance 判断）；未装 quickjs 时返回空元组。"""
    try:
        import _quickjs  # noqa: PLC0415  沙箱绑定，按需导入

        return _quickjs.Object
    except ImportError:
        return tuple  # type: ignore[return-value]


def probe_quickjs() -> tuple[bool, str]:
    """探测 JS 运行时可用性：import quickjs + 能力校验（Context、内存上限、执行看门狗）。

    python-quickjs-ng 与 PetterS/quickjs 的 import 名同为 quickjs——探测不承诺区分包
    来源（import 名冲突的已知限制），只保证「拿到的绑定具备所需能力」。
    """
    try:
        import quickjs  # noqa: PLC0415

        ctx = quickjs.Context()
        ok = (
            hasattr(ctx, "set_memory_limit")
            and hasattr(ctx, "set_time_limit")
        )
        if not ok:
            return False, ""
        try:
            from importlib.metadata import (
                PackageNotFoundError,  # noqa: PLC0415
                version,  # noqa: PLC0415
            )

            try:
                return True, str(version("python-quickjs-ng"))
            except PackageNotFoundError:
                return True, ""
        except Exception:  # noqa: BLE001 - 版本探测失败不影响可用性
            return True, ""
    except Exception:  # noqa: BLE001 - 未安装/装了不能用的绑定都算不可用
        return False, ""


# ---- 状态持久化（引擎代存；Mod 自身无写盘能力） ----


def _state_path(mod_id: str) -> Path:
    return mods_state_root() / f"{mod_id}.json"


def load_mod_state(mod_id: str) -> dict:
    """读 Mod 状态：{session_id: {...}}，单文件 ≤64KB、会话键 LRU 20。"""
    path = _state_path(mod_id)
    try:
        tf = read_text_file(path)
    except OSError:
        return {}
    if tf.binary or not tf.text.strip():
        return {}
    try:
        data = json.loads(tf.text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    # LRU 截断（读取时兜底）：顺序由写入侧保证——touch_mod_state 对刚更新的
    # 会话键做「先摘再插」，最近写入的键排在末尾，这里取尾部就是真 LRU
    trimmed: OrderedDict[str, dict] = OrderedDict()
    for key in list(data.keys())[-STATE_SESSION_LRU:]:
        if isinstance(data[key], dict):
            trimmed[str(key)] = data[key]
    return dict(trimmed)


def save_mod_state(mod_id: str, state: dict) -> None:
    """原子写 Mod 状态（write_text_atomic；超限截断最久未写的会话键）。"""
    trimmed: OrderedDict[str, dict] = OrderedDict()
    for key, value in state.items():
        if isinstance(value, dict):
            trimmed[str(key)] = value
    while trimmed:
        text = json.dumps(trimmed, ensure_ascii=False)
        if len(text.encode("utf-8")) <= MAX_STATE_BYTES:
            break
        trimmed.pop(next(iter(trimmed)), None)
    if not trimmed:
        # 空状态也落一个空对象文件（删除语义由 delete_mod_state 负责）
        write_text_atomic(_state_path(mod_id), "{}")
        return
    write_text_atomic(_state_path(mod_id), json.dumps(trimmed, ensure_ascii=False))


def delete_mod_state(mod_id: str) -> None:
    try:
        _state_path(mod_id).unlink(missing_ok=True)
    except OSError:
        pass


def touch_mod_state(mod_id: str, session_id: str, new_state: dict) -> None:
    """把一个会话键的更新写回状态文件，并把该键移到末尾（「LRU 20」的写入侧）。

    dict 保持插入序、更新既有键不移动位置——只改值不 re-insert 的话，
    超限截断会退化成「按创建序保留 20」，最早创建但仍在活跃的会话状态先被丢。
    这里先摘再插，让「最近写入」的键排在末尾。读改写与既有 save_mod_state
    路径一样没有跨进程锁（状态只用于展示，非关键数据）；进程内的并发串行
    由调用方负责——ModManager._run 经按 mod 的 asyncio.Lock 圈住整个读改写。
    """
    all_state = load_mod_state(mod_id)
    all_state.pop(session_id, None)
    all_state[session_id] = new_state
    save_mod_state(mod_id, all_state)


def read_mod_readme(mod_dir: Path) -> str:
    """读 Mod 的 README.md 正文（设置页展示，≤5000 字符截断）。"""
    try:
        tf = read_text_file(mod_dir / README_NAME)
    except OSError:
        return ""
    if tf.binary:
        return ""
    text = tf.text.strip()
    if len(text) > README_PREVIEW_CHARS:
        text = text[:README_PREVIEW_CHARS].rstrip() + "\n…（已截断）"
    return text


# ---- LoadedMod 与 ModManager ----


class LoadedMod:
    """一个已落盘的 Mod：清单 + 沙箱宿主 + 运行簿记（错误计数 / 最近执行 / 自动停用）。"""

    def __init__(self, mod_dir: Path, manifest: dict, *, source_label: str) -> None:
        self.dir = mod_dir
        self.manifest = manifest
        self.id: str = manifest["id"]
        self.source_label = source_label
        self.declared_hooks: set[str] = set(manifest["hooks"])
        self.detected_hooks: set[str] | None = None  # runtime 探测结果；None = 未探测
        self.load_error: str = ""
        self.warnings: list[str] = list(manifest.get("notes") or [])
        self.errors = 0
        self.consecutive_timeouts = 0
        self.auto_deactivated = False
        self.recent: deque[dict] = deque(maxlen=MAX_RECENT_RUNS)
        self._runtime: _ModRuntime | None = None
        self._probing: asyncio.Task | None = None

    # ---- 展示字段 ----

    @property
    def name(self) -> str:
        return self.manifest["name"]

    @property
    def permissions(self) -> str:
        return self.manifest["permissions"]

    @property
    def declarative(self) -> dict:
        return self.manifest["declarative"]

    @property
    def is_cc(self) -> bool:
        return bool(self.manifest["cc_mods"])

    def public_summary(self, *, active: bool) -> dict:
        """mods.list 的条目（清单 + 运行态摘要）。active = 总开关开着且在启用名单。"""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.manifest["version"],
            "description": self.manifest["description"],
            "author": self.manifest["author"],
            "enabled": active,
            "hooks": sorted(self.declared_hooks),
            "permissions": self.permissions,
            "declarative": dict(self.declarative),
            "source": self.source_label,
            "cc_mods": bool(self.is_cc),
            "cc_upstream": self.manifest["cc_mods"].get("upstream", ""),
            "errors": self.errors,
            "auto_deactivated": self.auto_deactivated,
            "load_error": self.load_error,
            "warnings": list(self.warnings),
            "recent": list(self.recent)[-5:],
        }

    # ---- 沙箱生命周期 ----

    async def _ensure_probed(self) -> bool:
        """确保 runtime 已创建并探测出实际实现的 hook；返回 JS handler 是否可用。"""
        if self.load_error:
            return False
        if self.detected_hooks is not None:
            return True
        if self._probing is None:
            self._probing = asyncio.get_running_loop().create_task(self._probe())
        await asyncio.shield(self._probing)
        return True

    async def _probe(self) -> None:
        try:
            source = self.dir / self.manifest["entry"]
            # 入口必须 resolve 后仍在 Mod 目录内（与安装预览同一道校验：
            # 清单是包作者自由填写的，entry 带穿越/盘符不得读目录外的文件）
            try:
                if not source.resolve().is_relative_to(self.dir.resolve()):
                    raise ModError(f"入口指向 Mod 目录之外，已拒绝：{self.manifest['entry']}")
            except OSError as e:
                raise ModError(f"入口路径不可解析：{e}") from e
            if not source.is_file():
                raise ModError(f"入口文件不存在：{self.manifest['entry']}")
            runtime = _ModRuntime(self.id, source)
            self.detected_hooks = await runtime.setup()
            self._runtime = runtime
            # 声明未实现的 hook：加载时警告（设置页可见，不静默）
            missing = sorted(self.declared_hooks - self.detected_hooks)
            for hook in missing:
                self.warnings.append(f"清单声明了 {hook}，但 main.js 里没有对应的 handler")
        except Exception as e:  # noqa: BLE001 - 装载失败进设置页，不影响其它 Mod
            self.load_error = f"装载失败：{e}"
            self.detected_hooks = set()
            if self._runtime is not None:
                self._runtime.close()
                self._runtime = None
        finally:
            self._probing = None

    def dispatch_hooks(self) -> set[str]:
        """本次会实际执行的 hook 集合（声明 ∩ 实现；清单未声明时用实现集，CC 兼容）。"""
        detected = self.detected_hooks if self.detected_hooks is not None else set()
        if not self.declared_hooks:
            return set(detected)
        return set(self.declared_hooks) & detected

    def _record(self, hook: str, status: str, duration_ms: int, output: str = "") -> None:
        self.recent.append({
            "time": time.time(),
            "hook": hook,
            "status": status,
            "duration_ms": duration_ms,
            "output": output[:_SUMMARY_CHARS],
        })

    async def run_hook(
        self, hook: str, payload: dict, state: dict, *, record: bool = True,
    ) -> tuple[object | None, dict, str]:
        """执行一个 handler，返回 (原始结果, 新状态, 错误信息)。超时计入自动停用。

        record=False 供测试器使用：实跑但不进「最近执行」记录（test_hook 同款）。
        """
        if self._runtime is None:
            await self._ensure_probed()
            if self._runtime is None:
                return None, state, self.load_error or "JS 运行时不可用"
        t0 = time.monotonic()
        try:
            raw, new_state = await asyncio.wait_for(
                self._runtime.call(hook, payload, state),
                timeout=HANDLER_HARD_DEADLINE_S,
            )
        except TimeoutError:
            # 绑定层僵死（正常时 set_time_limit 会先打断）：视同超时处理，
            # runtime 已不可信，标记自动停用（重启复活）
            self.consecutive_timeouts += 1
            if record:
                self._record(hook, "timeout", int((time.monotonic() - t0) * 1000), "执行超过硬顶")
            if self.consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                self.auto_deactivated = True
                self._close_runtime()
            return None, state, f"{hook} 执行超时（>{HANDLER_HARD_DEADLINE_S:.0f}s）"
        except Exception as e:  # noqa: BLE001 - handler 抛错（含超时中断）不影响主流程
            duration = int((time.monotonic() - t0) * 1000)
            message = str(e).split("\n")[0][:160]
            if "interrupted" in message.lower():
                self.consecutive_timeouts += 1
                if record:
                    self._record(hook, "timeout", duration, f"{hook} 超过 {HANDLER_TIME_LIMIT_S:.1f}s 被中断")
                if self.consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                    self.auto_deactivated = True
                    self._close_runtime()
                return None, state, f"{hook} 执行超时（>{HANDLER_TIME_LIMIT_S:.1f}s 已中断）"
            self.consecutive_timeouts = 0
            if record:
                self._record(hook, "error", duration, message)
            return None, state, f"{hook} 执行出错：{message}"
        self.consecutive_timeouts = 0
        if record:
            self._record(hook, "ok", int((time.monotonic() - t0) * 1000))
        return raw, new_state, ""

    def _close_runtime(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
        self.detected_hooks = None  # 下次启用重新探测

    def close(self) -> None:
        self._close_runtime()


def mods_config_from_raw(raw: dict) -> tuple[bool, list[str]]:
    """从 config.toml 原始数据读 [mods] 表：返回 (总开关, 启用名单)。"""
    section = raw.get("mods") if isinstance(raw.get("mods"), dict) else {}
    enabled = bool(section.get("enabled", False))
    ids_raw = section.get("enabled_mods") or []
    ids: list[str] = []
    if isinstance(ids_raw, list):
        for item in ids_raw:
            text = str(item or "").strip()
            if text and text not in ids:
                ids.append(text)
    return enabled, ids


class ModManager:
    """Mod 装载、启停与事件分发（进程内单例，backend 持有）。

    Agent 侧只接触三个面：``declarative_deny``（门前纯 Python 查询）、
    ``extra_confirm``（门内收紧查询，只能返回 True）与 ``on_*`` 系列 JS 分发
    （全部在门后）。没有 allow / modify 语义的任何出口。
    """

    def __init__(self, *, enabled: bool = False, enabled_ids: list[str] | None = None,
                 root: Path | None = None) -> None:
        self.enabled = enabled
        self.enabled_ids: list[str] = list(enabled_ids or [])
        self.root = Path(root) if root else mods_root()
        self.runtime_available, self.runtime_version = probe_quickjs()
        self.mods: OrderedDict[str, LoadedMod] = OrderedDict()
        # 按 mod id 的状态写锁（_run 的「重读全量 → 合并 → 原子落盘」临界区用）。
        # 按需创建，键集只随「写过状态的 mod」增长，量级 ≤ 已装载 mod 数（个位到
        # 几十个），不做清理——生命周期与 ModManager 实例同寿：本进程只有这一个
        # 实例被基础 Agent 与所有会话 Agent 共享引用（_reload_mods 重建管理器时，
        # 锁字典随旧实例整体丢弃，新实例另起）。锁只圈状态 IO，绝不跨 run_hook
        # 的 JS 执行，持锁时长只有一次小文件读 + 原子写。
        self._state_locks: dict[str, asyncio.Lock] = {}
        self.load_all()

    # ---- 装载 ----

    def load_all(self) -> None:
        """扫描安装根目录，重建 LoadedMod 表（坏清单也进表展示错误，不静默消失）。"""
        self.mods = OrderedDict()
        try:
            entries = sorted(p for p in self.root.iterdir() if p.is_dir())
        except OSError:
            entries = []
        for path in entries:
            if path.name == "state" or path.name.startswith("."):
                continue
            mod_id = path.name
            try:
                manifest, _ = load_manifest_from_dir(path)
            except ModError as e:
                broken = LoadedMod(path, {
                    "id": mod_id, "name": mod_id, "version": "", "description": "",
                    "author": "", "entry": "", "api": 1, "hooks": [],
                    "permissions": "observe",
                    "declarative": {"deny_tools": [], "require_confirm_tools": []},
                    "cc_mods": {}, "notes": [],
                }, source_label="未知")
                broken.load_error = str(e)
                self.mods[mod_id] = broken
                continue
            self.mods[manifest["id"]] = LoadedMod(
                path, manifest, source_label="官方示例" if self._is_official(path) else "本地安装",
            )

    @staticmethod
    def _is_official(mod_dir: Path) -> bool:
        """是否官方示例：只认 install_official_mod 落盘的 .source.json（source=bundled）。

        清单里的 author / cc_mods.upstream 是包作者的自由填写字段，第三方 Mod
        把自己标成「SkySheep」即可骗取官方信任指示——不作为判定依据（原文仍会
        在设置页展示，但只当普通文本）。没有 .source.json（老版本装的、手工拷入
        的）一律按「本地安装」。
        """
        try:
            tf = read_text_file(mod_dir / ".source.json")
        except OSError:
            return False
        if tf.binary:
            return False
        try:
            data = json.loads(tf.text)
        except ValueError:
            return False
        return isinstance(data, dict) and data.get("source") == "bundled"

    def close(self) -> None:
        for mod in self.mods.values():
            mod.close()

    # ---- 启用状态 ----

    def reload_config(self, *, enabled: bool, enabled_ids: list[str]) -> None:
        """用户改了 config 后同步总开关与启用名单（_reload_mods 用）。"""
        self.enabled = enabled
        self.enabled_ids = list(enabled_ids)
        # 重新启用时清掉上一进程周期的连续超时计数；自动停用标记保留与否按
        # 「本进程内」语义：关掉总开关再打开视为同一进程周期，不自动清除
        for mod in self.mods.values():
            if mod.id not in self.enabled_ids:
                mod.consecutive_timeouts = 0

    def id_active(self, mod_id: str) -> bool:
        """该 Mod 当前是否被启用且总开关开着（不含进程内自动停用判断）。"""
        return self.enabled and mod_id in self.enabled_ids

    # ---- 收紧声明（纯 Python 求值，不依赖 JS runtime） ----

    def _active_mods(self) -> list[LoadedMod]:
        """启用中的 Mod（含自动停用的——declarative 是静态表，停 JS 不停声明）。"""
        return [m for m in self.mods.values() if self.id_active(m.id)]

    @property
    def has_declarative(self) -> bool:
        return any(
            m.declarative.get("deny_tools") or m.declarative.get("require_confirm_tools")
            for m in self._active_mods()
        )

    async def has_tool_pre(self) -> bool:
        """启用中的 Mod 有实际会执行的 tool_pre JS handler（并发只读批要不要断批）。

        与 dispatch_hooks 同口径：声明了 tool_pre 直接算；CC 风格清单未声明
        hooks 的 Mod 按实现集（detected）判断——先懒探测再查。只看声明集会
        漏掉这批 Mod（声明空时分发走实现集），并发批的成员又不过 on_tool_pre，
        等于「串行路径生效、批内成员可绕过」。
        """
        for m in self._active_mods():
            if m.auto_deactivated or not self.runtime_available:
                continue
            if "tool_pre" in m.declared_hooks:
                return True
            if not m.declared_hooks:
                await m._ensure_probed()
                if "tool_pre" in m.dispatch_hooks():
                    return True
        return False

    def declarative_deny(self, tool_name: str, input_dict: dict) -> str:
        """deny_tools 门前查询：命中返回拒因（给模型与界面的 deny 文案），否则空串。"""
        for mod in self._active_mods():
            patterns = mod.declarative.get("deny_tools") or []
            if any(fnmatch(tool_name, pat) for pat in patterns):
                return f"[mod:{mod.id}] 该工具已被 Mod「{mod.name}」的收紧声明拒绝（deny_tools）"
        return ""

    def requires_confirm(self, tool_name: str) -> bool:
        """require_confirm_tools 门内查询：命中 = 该调用必须逐次确认。"""
        for mod in self._active_mods():
            patterns = mod.declarative.get("require_confirm_tools") or []
            if any(fnmatch(tool_name, pat) for pat in patterns):
                return True
        return False

    def describe_confirm(self, tool_name: str) -> str:
        """命中 require_confirm 的 Mod 说明（权限卡 note 用，用户看得见为什么多一次确认）。"""
        parts: list[str] = []
        for mod in self._active_mods():
            patterns = mod.declarative.get("require_confirm_tools") or []
            if any(fnmatch(tool_name, pat) for pat in patterns):
                parts.append(f"Mod {mod.id}")
        return "、".join(dict.fromkeys(parts))

    def extra_confirm(self, tool: object, input_dict: dict) -> bool:
        """PermissionGate.extra_confirm 契约：命中收紧声明才返回 True。

        **该接口没有「返回 False 来放行」的语义**——False / None / 异常一律
        「不干预」，门按原逻辑走（只收紧不放松的结构保证之一）。
        """
        if not self.enabled:
            return False
        try:
            return self.requires_confirm(getattr(tool, "name", ""))
        except Exception:  # noqa: BLE001 - 查询失败按不干预
            return False

    # ---- JS 分发（全部在权限门之后） ----

    async def _dispatchable(self, hook: str) -> list[LoadedMod]:
        """本次会实际执行该 hook 的启用 Mod（懒探测 runtime 后按「声明 ∩ 实现」过滤）。"""
        if not self.enabled or not self.runtime_available:
            return []
        out: list[LoadedMod] = []
        for mod in self._active_mods():
            if mod.auto_deactivated:
                continue
            if mod.detected_hooks is None:
                await mod._ensure_probed()  # 首次分发前探测实际实现的 handler
            if hook in mod.dispatch_hooks():
                out.append(mod)
        return out

    @staticmethod
    def _prefixed(mod: LoadedMod, text: str) -> str:
        return f"[Mod·{mod.id}] {text}"[: MAX_MOD_NOTE_CHARS + 20]

    def _state_lock(self, mod_id: str) -> asyncio.Lock:
        """取该 Mod 的状态写锁（按需创建，生命周期见 __init__ 注释）。

        锁串行化的是 touch_mod_state 内部的「重读全量 → 摘插本会话键 → 原子写回」：
        同一 Mod 被基础 Agent 与会话 Agent 并发派发时，两条 _run 若都在对方落盘
        之前完成重读，后写者会拿旧快照整键覆盖先写者的会话状态（last-writer-wins，
        先写者的会话键丢失）。run_hook 的 JS 执行 await 留在锁外——绝不持锁等 JS。
        """
        lock = self._state_locks.get(mod_id)
        if lock is None:
            lock = asyncio.Lock()
            self._state_locks[mod_id] = lock
        return lock

    async def _run(
        self, mod: LoadedMod, hook: str, payload: dict, session_id: str,
        *, allow_deny: bool, errors: list[str],
    ) -> dict:
        """跑一个 Mod 的一个 hook：状态进出 + 动作白名单解析。

        该 Mod 在这次执行里贡献的全部错误（执行失败 / 状态落盘失败 / 动作解析
        丢弃）计入 ``mod.errors``；状态落盘失败与 run_hook 外的意外异常也各补
        一条 "error" 执行记录，解析类丢弃另追加一条 "dropped"——设置页的错误
        计数与「最近执行」由此如实可见，不再只见 handler 的 ✓。
        """
        errors_before = len(errors)
        state = load_mod_state(mod.id).get(session_id, {})
        try:
            raw, new_state, error = await mod.run_hook(hook, payload, state)
        except Exception as e:  # noqa: BLE001 - 状态 IO 等意外也不阻断主流程
            errors.append(f"{mod.id}: {e}")
            mod.errors += 1
            # run_hook 没跑到自己的记数逻辑，这里补一条执行记录，
            # 让「错误 N」chip 指引的明细在最近执行里查得到
            mod._record(hook, "error", 0, str(e))
            return {"deny": None, "note": "", "ui": []}
        if error:
            errors.append(f"{mod.id}: {error}")
            if mod.auto_deactivated:
                errors.append(f"{mod.id}: 连续超时 {MAX_CONSECUTIVE_TIMEOUTS} 次，已在本进程内自动停用")
            mod.errors += len(errors) - errors_before
            return {"deny": None, "note": "", "ui": []}
        if new_state != state:
            try:
                # 临界区（按 mod 串行）：touch_mod_state 内部是「重读全量 → 摘插
                # 本会话键 → 原子写回」，并发派发同一 Mod 时两键都不得丢。
                # run_hook 的 await 在上面、锁外，持锁只有一次小文件读写
                async with self._state_lock(mod.id):
                    touch_mod_state(mod.id, session_id, new_state)
            except Exception as e:  # noqa: BLE001 - 状态写盘失败不影响主流程
                errors.append(f"{mod.id}: 状态保存失败：{e}")
                # handler 本身跑成功（run_hook 已记 "ok"），与 dropped 同款
                # 补一条出错记录：错误计数与「最近执行」对得上
                mod._record(hook, "error", 0, f"状态保存失败：{e}")
        parse_before = len(errors)
        result = _parse_action_result(
            raw, hook=hook, allow_deny=allow_deny and mod.permissions == "tighten",
            mod_id=mod.id, errors=errors,
        )
        if len(errors) > parse_before:
            # handler 本身跑成功（run_hook 已记 "ok"），但动作被解析层丢弃：
            # 追加一条 dropped 记录让「为什么 deny 没生效」在设置页可查
            mod._record(hook, "dropped", 0, "；".join(errors[parse_before:]))
        mod.errors += len(errors) - errors_before
        return result

    async def on_permission_request(
        self, tool_name: str, input_dict: dict, safety: str, detail: str, session_id: str,
    ) -> dict:
        """权限请求附加信息/否决（挂点 1）。返回 {denied, deny_note, note, ui, errors}。

        veto（denied=True）是「替用户提前拒绝」——收紧向；用户可随时在设置页
        停用该 Mod 恢复原确认流。
        """
        errors: list[str] = []
        merged = {"denied": False, "deny_note": "", "note": "", "ui": []}
        for mod in await self._dispatchable("permission_request"):
            payload = {
                "tool": tool_name, "input": input_dict,
                "safety": safety, "detail": detail, "sessionId": session_id,
            }
            result = await self._run(mod, "permission_request", payload, session_id,
                                     allow_deny=True, errors=errors)
            merged["ui"].extend(
                dict(w, mod_id=mod.id) for w in result["ui"] if w["slot"] == "perm"
            )
            if result["deny"] and not merged["denied"]:
                merged["denied"] = True
                merged["deny_note"] = self._prefixed(mod, result["deny"])
                continue  # 已否决：不再附加该 Mod 的 note
            if result["note"]:
                merged["note"] = (
                    (merged["note"] + "\n" if merged["note"] else "")
                    + self._prefixed(mod, result["note"])
                )
        merged["errors"] = errors
        return merged

    async def on_tool_pre(
        self, tool_name: str, input_dict: dict, session_id: str, context_tokens: int,
    ) -> dict:
        """工具调用前（挂点 2）。返回 {deny, note, ui, errors}；参数不可变，无 modify。"""
        errors: list[str] = []
        merged: dict[str, Any] = {"deny": None, "note": "", "ui": []}
        for mod in await self._dispatchable("tool_pre"):
            payload = {
                "tool": tool_name, "input": input_dict,
                "sessionId": session_id, "contextTokens": context_tokens,
            }
            result = await self._run(mod, "tool_pre", payload, session_id,
                                     allow_deny=True, errors=errors)
            merged["ui"].extend(dict(w, mod_id=mod.id) for w in result["ui"])
            if result["deny"] and merged["deny"] is None:
                merged["deny"] = self._prefixed(mod, result["deny"])
                continue
            if result["note"]:
                merged["note"] = (
                    (merged["note"] + "\n" if merged["note"] else "")
                    + self._prefixed(mod, result["note"])
                )
        merged["errors"] = errors
        return merged

    async def on_tool_post(
        self, tool_name: str, input_dict: dict, result_preview: str, is_error: bool,
        duration_ms: int, session_id: str, context_tokens: int,
        context_limit_tokens: int = 0,
    ) -> dict:
        """工具调用后（挂点 4，两路收割点）。只观察：note / ui，不能改结果与历史。"""
        errors: list[str] = []
        merged: dict[str, Any] = {"notes": [], "ui": []}
        for mod in await self._dispatchable("tool_post"):
            payload = {
                "tool": tool_name, "input": input_dict,
                "resultPreview": result_preview, "isError": bool(is_error),
                "durationMs": int(duration_ms),
                "sessionId": session_id, "contextTokens": context_tokens,
                "contextLimitTokens": int(context_limit_tokens),
            }
            result = await self._run(mod, "tool_post", payload, session_id,
                                     allow_deny=False, errors=errors)
            merged["ui"].extend(dict(w, mod_id=mod.id) for w in result["ui"])
            if result["note"]:
                merged["notes"].append(self._prefixed(mod, result["note"]))
        merged["errors"] = errors
        return merged

    async def on_iteration_start(self, session_id: str, iteration: int) -> dict:
        """迭代开始（挂点 5）。一次用户提交会触发多次（每次模型调用一个迭代）；仅 ui。"""
        errors: list[str] = []
        ui: list[dict] = []
        for mod in await self._dispatchable("iteration_start"):
            payload = {"sessionId": session_id, "iteration": int(iteration)}
            result = await self._run(mod, "iteration_start", payload, session_id,
                                     allow_deny=False, errors=errors)
            ui.extend(dict(w, mod_id=mod.id) for w in result["ui"])
        return {"ui": ui, "errors": errors}

    async def on_turn_stop(
        self, stop_reason: str, iterations: int, session_id: str, context_tokens: int,
        context_limit_tokens: int = 0,
    ) -> dict:
        """回合结束（挂点 6）。stop_reason ∈ {end_turn, max_iterations, error}；仅 ui。"""
        errors: list[str] = []
        ui: list[dict] = []
        for mod in await self._dispatchable("turn_stop"):
            payload = {
                "stopReason": stop_reason, "iterations": int(iterations),
                "sessionId": session_id, "contextTokens": context_tokens,
                "contextLimitTokens": int(context_limit_tokens),
            }
            result = await self._run(mod, "turn_stop", payload, session_id,
                                     allow_deny=False, errors=errors)
            ui.extend(dict(w, mod_id=mod.id) for w in result["ui"])
        return {"ui": ui, "errors": errors}


# ---- 官方示例包（mods-gallery，照 skills/gallery.py 的 bundled 模式） ----

# 打包内示例目录：SkySheep.spec 从仓库根 mods-gallery/ 逐文件收进来，落位
# src/skysheep/mods/gallery/<dir>/。源码/wheel 运行时该目录不存在（开发态回落
# 仓库根的 mods-gallery/），bundled_gallery_dir 返回 None → 官方一键装提示改用
# 本地安装。目录里只有数据文件、没有 __init__.py，不会被当作 skysheep.mods 包导入。
GALLERY_BUNDLE_DIR = Path(__file__).resolve().parents[1] / "mods" / "gallery"
# 开发态回落：core/mods.py → SkySheep（仓库根）/ mods-gallery/
GALLERY_REPO_DIR = Path(__file__).resolve().parents[4] / "mods-gallery"


def bundled_gallery_dir() -> Path | None:
    """官方示例的可用目录（打包内副本优先，开发态回落仓库根）；不可用返回 None。"""
    if (GALLERY_BUNDLE_DIR / "manifest.json").is_file():
        return GALLERY_BUNDLE_DIR
    if (GALLERY_REPO_DIR / "manifest.json").is_file():
        return GALLERY_REPO_DIR
    return None


def load_gallery_manifest() -> list[dict]:
    """读官方示例清单；读取/解析失败返回空列表。"""
    root = bundled_gallery_dir()
    if root is None:
        return []
    try:
        tf = read_text_file(root / "manifest.json")
    except OSError:
        return []
    try:
        data = json.loads(tf.text)
    except ValueError:
        return []
    mods = data.get("mods") if isinstance(data, dict) else None
    if not isinstance(mods, list):
        return []
    return [m for m in mods if isinstance(m, dict) and str(m.get("id", "")).strip()]


# ---- 「让 AI 帮我写一个 Mod」内置提示词模板 ----

MOD_WRITING_TEMPLATE = """请帮我编写一个 SkySheep Mod（实验性扩展）。SkySheep 的 Mod 是一个文件夹，
包含三个文件：mod.json（清单）、main.js（入口，单文件 ≤128KB）、README.md（说明）。

请按下面的规格输出完整内容（我会保存后手动安装）：

1. mod.json 字段（全部可用的字段就这些，未知字段会被拒绝）：
   - id：目录名，只能用字母/数字/连字符/下划线，不能有路径分隔符、冒号、纯点
   - name / version / description / author：展示用文本
   - entry：固定 "main.js"
   - api：固定 1
   - hooks：声明要挂的事件，只能取 tool_pre / tool_post / permission_request /
     iteration_start / turn_stop
   - permissions：只能 "observe"（只能观察与提示）或 "tighten"（还可以拒绝 +
     声明收紧表）；声明 declarative 必须 tighten
   - declarative（可选 tighten 档）：{"deny_tools": ["工具名通配"],
     "require_confirm_tools": ["工具名通配"]}——纯静态收紧声明
2. main.js 契约（SkySheep Mod API v1）：
   export default {
     toolPre({ tool, input, sessionId, contextTokens }) { ... },      // 返回 {deny?, note?, ui?}
     toolPost({ tool, input, resultPreview, isError, durationMs, ... }) { ... },  // 返回 {note?, ui?}
     permissionRequest({ tool, input, safety, detail, sessionId }) { ... },  // 返回 {deny?, note?, ui?}
     iterationStart({ sessionId, iteration }) { ... },                // 返回 {ui?}
     turnStop({ stopReason, iterations, sessionId, contextTokens }) { ... },  // 返回 {ui?}
   };
   - 动作闭集只有 {deny, note, ui}：deny 只在 tighten 档有效；**没有 allow、
     没有 modifyInput**——Mod 不能放行权限、不能修改工具参数或结果
   - 可用全局只有 sky.state（本 Mod、本会话键的持久 JSON 对象，引擎代存）与
     sky.now()；**没有任何文件 / 网络 / 进程 API**，也不能 import 任何东西
   - ui 是声明式小部件数组，五种固定形状（kind/slot 固定搭配，未知形状会被丢弃）：
     {kind:"stat", slot:"tray", text, level:"ok"|"warn"|"storm"}
     {kind:"badge", slot:"stream"|"perm", text}
     {kind:"progress", slot:"stream", value:0-100, text}
     {kind:"timeline", slot:"stream", items:[{t, text}]}
     {kind:"text", slot:"perm", text}   // ≤500 字符，权限卡上的影响面说明
3. 安全边界（必须遵守）：Mod 对权限门只能收紧（拒绝/要求确认），不能自动放行；
   只能观察事件与产出展示片段。

我的需求是：（在这里描述你想让 Mod 做什么）
"""


def mods_template() -> str:
    """「让 AI 写 Mod」提示词模板（mods.get_template 下发，前端不写长文本）。"""
    return MOD_WRITING_TEMPLATE
