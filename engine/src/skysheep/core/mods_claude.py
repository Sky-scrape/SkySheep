"""Claude Code Mods 兼容层（P2）。

与 core/mods.py 的关系：

- mods.py：旧实现——quickjs 沙箱、5 hook、只能收紧。
- mods_claude.py：新实现——Node.js 无沙箱、中间件链、可 allow/ask/deny/rewrite。
  **完全对齐 Claude Code Mods 的语义**。

两个实现共存：同一个 ModManager 可以被宿主切换为任意一种，测试与灰度用。
本模块是「新实现」，旧路径不改。

关键契约：

- 权限决策（tool.check）：handler 返回
  * undefined/null → 不干预（继续链/走原授权）
  * {decision: 'allow' | 'ask' | 'deny', reason?, input?} → 生效
  * input 出现表示 rewrite（工具参数被 Mod 改写）
- 中间件语义与 Node 运行时（mods_runtime.mjs）一致：
  * handler 不调 next() 则中断链，下游不执行
  * 调 next() 则下游执行；返回值向外冒泡
- 事件：45+ 事件名（与 Claude Code 对齐），暂只落 5 个，其余事件透传。

本模块**不做**的事（P3+ 交付）：
- 元素树 UI 渲染（Pane / AbovePrompt / spinner 等）
- $.tool.register / $.command.register / $.model.complete / $.mcp.*
- 插件市场安装（.claude-plugin/plugin.json）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..messages import Message
from ..models.base import ProviderDone, ProviderTextDelta
from ..textio import read_text_file, write_text_atomic
from .mods_node import NodeRuntime, NodeRuntimeError, probe_node
from .mods_ui import normalize_ui_tree

logger = logging.getLogger("skysheep.mods_claude")

# Claude Code Mods 的核心事件名（暂落 5 个，其余在 P3 扩展）
CORE_EVENTS = (
    "tool.check",            # 权限决策（最重要，可 allow/ask/deny/rewrite）
    "tool.pre",              # 工具调用前
    "tool.post",             # 工具调用后
    "prompt.submit",         # 用户提交一条消息
    "turn.stop",             # 一轮结束
    "session.start",
    "session.end",
    "iteration.start",
)

# 权限决策常量
DECISION_ALLOW = "allow"
DECISION_ASK = "ask"
DECISION_DENY = "deny"

STATE_FILE = "state.json"
# 清单查找顺序（先命中先用）：
#   1. .claude-plugin/plugin.json —— Claude Code 插件标准位置
#   2. mod.json                    —— SkySheep 旧格式（也用作 CC 兼容别名）
#   3. mods.json                   —— Claude Code Mods 别名
MANIFEST_CANDIDATES = (
    ".claude-plugin/plugin.json",
    "mod.json",
    "mods.json",
)

# plugin.json 缺少 entry 时的入口候选（相对 Mod 根）
ENTRY_CANDIDATES = ("hooks/main.js", "main.js", "index.js", "hooks/index.js")


def _find_manifest(mod_dir: Path) -> tuple[Path | None, dict | None]:
    """按 MANIFEST_CANDIDATES 顺序找到第一份可解析的清单。"""
    for rel in MANIFEST_CANDIDATES:
        path = mod_dir / rel
        if not path.is_file():
            continue
        try:
            tf = read_text_file(path)
            if tf.binary or not tf.text.strip():
                continue
            data = json.loads(tf.text)
        except (OSError, ValueError) as e:
            logger.warning("[mods] 读取清单失败 %s: %s", path, e)
            continue
        if isinstance(data, dict):
            return path, data
    return None, None


def _normalize_manifest(mod_dir: Path, data: dict) -> dict:
    """把 plugin.json / mod.json / mods.json 归一成内部清单 dict。

    两种风格的映射：
      - SkySheep 风格：直接读 id/name/version/entry/hooks/permissions
      - Claude Code 风格（plugin.json）：name → id（小写化）、无 entry 时按
        ENTRY_CANDIDATES 猜、hooks 缺省时视为全事件（register 决定实际）
    """
    out: dict = {}
    # id：优先显式 id，否则 name 规范化（小写、非标识符字符换 -）
    raw_id = str(data.get("id") or "").strip()
    if not raw_id:
        raw_name = str(data.get("name") or mod_dir.name).strip()
        import re as _re
        raw_id = _re.sub(r"[^A-Za-z0-9_-]+", "-", raw_name).strip("-").lower() or mod_dir.name
    out["id"] = raw_id
    out["name"] = str(data.get("name") or raw_id)
    out["version"] = str(data.get("version") or "0.0.0")
    out["description"] = str(data.get("description") or "")

    # entry：显式 > main 字段 > ENTRY_CANDIDATES 里第一个存在的
    entry = str(data.get("entry") or data.get("main") or "").strip()
    if not entry:
        for cand in ENTRY_CANDIDATES:
            if (mod_dir / cand).is_file():
                entry = cand
                break
    out["entry"] = entry or "main.js"

    hooks_raw = data.get("hooks")
    if isinstance(hooks_raw, list):
        out["events"] = [str(h) for h in hooks_raw if str(h or "").strip()]
    elif isinstance(data.get("events"), list):
        out["events"] = [str(h) for h in data["events"] if str(h or "").strip()]
    else:
        # 未声明时视为全事件：具体有哪些由 main.js 里 register() 决定
        out["events"] = list(CORE_EVENTS)

    out["permissions"] = str(data.get("permissions") or "full")
    return out


def _state_path(mod_dir: Path) -> Path:
    return mod_dir / STATE_FILE


class ClaudeModError(RuntimeError):
    """加载/调用 Mod 失败。"""


class ClaudeLoadedMod:
    """一个已加载的 Claude Code Mod：manifest + Node 运行时 + 状态。"""

    def __init__(self, mod_dir: Path, manifest: dict, host_handler=None) -> None:
        self.dir = Path(mod_dir)
        self.manifest = manifest
        self.id: str = str(manifest.get("id") or mod_dir.name)
        self.name: str = str(manifest.get("name") or self.id)
        self.version: str = str(manifest.get("version") or "0.0.0")
        self.entry: str = str(manifest.get("entry") or "main.js")
        self.permissions: str = str(manifest.get("permissions") or "full")
        self.events: list[str] = list(manifest.get("events") or CORE_EVENTS)
        self._runtime: NodeRuntime | None = None
        self._state_cache: dict = {}
        self._state_loaded = False
        self.load_error: str = ""
        # host_handler 由 manager 注入：处理 $.model/$.mcp/$.store/... 的反向调用
        self._host_handler = host_handler
        # 已注册的工具/命令（P4 只记录，工具真接入注册表需 backend 拉取）
        self.registered_tools: list[dict] = []
        self.registered_commands: list[dict] = []

    @property
    def entry_path(self) -> Path:
        return self.dir / self.entry

    def public_summary(self, *, active: bool) -> dict:
        """mods.list 的条目（与 quickjs LoadedMod.public_summary 同形）。

        与旧模式的字段差异：
        - declarative 恒为空（Claude 模式用 tool.check JS 决策，不靠静态收紧表）
        - cc_mods 恒为 True（本模式的 Mod 全是 CC 插件格式）
        - source 读 .source.json（官方示例 vs 本地安装）
        - recent 恒为空（Claude 模式不维护执行历史环形）

        保持同形是为了让前端 renderModsCfg 不区分两套清单，字段缺一块就空着。
        """
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": str(self.manifest.get("description") or ""),
            "author": str(self.manifest.get("author") or ""),
            "enabled": active,
            "hooks": sorted(self.events),
            "permissions": self.permissions,
            "declarative": {"deny_tools": [], "require_confirm_tools": []},
            "source": self._source_label(),
            "cc_mods": True,
            "cc_upstream": "",
            "errors": 0,
            "auto_deactivated": False,
            "load_error": self.load_error,
            "warnings": [],
            "recent": [],
        }

    def _source_label(self) -> str:
        """从 .source.json 读安装来源（官方示例 / 本地安装；读不到一律本地）。"""
        try:
            path = self.dir / ".source.json"
            if not path.is_file():
                return "本地安装"
            tf = read_text_file(path)
            if tf.binary:
                return "本地安装"
            data = json.loads(tf.text)
            if isinstance(data, dict) and data.get("source") == "bundled":
                return "官方示例"
            return "本地安装"
        except (OSError, ValueError):
            return "本地安装"

    async def ensure_loaded(self, session_id: str = "") -> bool:
        """懒加载 Node 运行时；失败时只设置 load_error，不抛。"""
        if self._runtime is not None:
            return True
        if self.load_error:
            return False
        try:
            rt = NodeRuntime(
                self.id, self.entry_path, mod_dir=self.dir, session_id=session_id,
                host_handler=self._host_handler,
            )
            await rt.start()
            self._runtime = rt
            return True
        except (NodeRuntimeError, OSError) as e:
            self.load_error = str(e)
            return False

    def _load_state(self) -> dict:
        if self._state_loaded:
            return self._state_cache
        try:
            tf = read_text_file(_state_path(self.dir))
        except OSError:
            self._state_cache = {}
            self._state_loaded = True
            return {}
        if tf.binary or not tf.text.strip():
            self._state_cache = {}
            self._state_loaded = True
            return {}
        try:
            data = json.loads(tf.text)
            self._state_cache = data if isinstance(data, dict) else {}
        except ValueError:
            self._state_cache = {}
        self._state_loaded = True
        return self._state_cache

    def _save_state(self) -> None:
        try:
            write_text_atomic(_state_path(self.dir), json.dumps(self._state_cache, ensure_ascii=False))
        except OSError as e:
            logger.warning("[mods:%s] 保存状态失败：%s", self.id, e)

    async def run(self, event: str, payload: dict, session_id: str = "") -> tuple[object, str]:
        """跑一次事件链，返回 (verdict, error_message)。"""
        if not await self.ensure_loaded(session_id):
            return None, self.load_error or "未加载"
        assert self._runtime is not None
        state = self._load_state()
        verdict, new_state, err = await self._runtime.call(event, payload, state, session_id=session_id)
        if new_state and new_state != state:
            self._state_cache = new_state
            self._save_state()
        return verdict, err

    async def close(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None


class ClaudeModManager:
    """Claude Code Mods 兼容模式的管理器。

    与旧 ModManager 的接口差异：

    - on_tool_check(tool_name, input_dict, session_id) -> dict
      返回 {"decision": allow|ask|deny|None, "reason": str, "input": dict|None, "errors": list}
    - on_event(event, payload, session_id) -> list[dict]
      返回各 Mod 的 verdict 列表（供调用方合并）
    """

    def __init__(self, *, enabled: bool = False, enabled_ids: list[str] | None = None,
                 root: Path | None = None) -> None:
        self.enabled = enabled
        self.enabled_ids = list(enabled_ids or [])
        if root:
            self.root = Path(root)
        else:
            # 延迟导入避免 config <-> core 循环；路径与旧 mods 同根
            from ..config import skysheep_home

            self.root = skysheep_home() / "mods"
        self.mods: dict[str, ClaudeLoadedMod] = {}
        self.node_available, self.node_version = probe_node()
        # 引擎能力注入点（backend 在 _reload_mods 时填）：
        # provider_getter: () -> Provider | None
        # mcp_manager:    MCPManager | None
        # settings_getter: () -> dict
        self.provider_getter = None
        self.mcp_manager = None
        self.settings_getter = None
        # 工具注册变化回调（backend 注入）：Mod 注册新工具后重建注册表，
        # 否则模型要等下一次 _apply_registry_to_agents 才看得见
        self.on_tools_changed = None

    # ---- 加载 ----

    def load_all(self) -> None:
        """扫描 root 下所有子目录，读清单（.claude-plugin/plugin.json / mod.json / mods.json）。"""
        self.mods.clear()
        if not self.root.is_dir():
            return
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            manifest_path, raw = _find_manifest(child)
            if manifest_path is None or raw is None:
                continue
            manifest = _normalize_manifest(child, raw)
            mod = ClaudeLoadedMod(
                child, manifest,
                host_handler=self._make_host_handler(manifest["id"]),
            )
            self.mods[mod.id] = mod

    def _active(self) -> list[ClaudeLoadedMod]:
        if not self.enabled:
            return []
        return [m for m in self.mods.values() if m.id in self.enabled_ids]

    # ---- 引擎桥：Node 反向调用 ----

    def _make_host_handler(self, mod_id: str):
        """为某个 Mod 构造 host_handler 闭包（绑定 mod_id，隔离 store）。"""
        async def handler(method: str, params: dict) -> object:
            return await self._dispatch_host(mod_id, method, params)
        return handler

    def _store_path(self, mod_id: str) -> Path:
        return self.root / mod_id / "store.json"

    def _read_store(self, mod_id: str) -> dict:
        try:
            tf = read_text_file(self._store_path(mod_id))
        except OSError:
            return {}
        if tf.binary or not tf.text.strip():
            return {}
        try:
            data = json.loads(tf.text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_store(self, mod_id: str, data: dict) -> None:
        write_text_atomic(self._store_path(mod_id), json.dumps(data, ensure_ascii=False, default=str))

    async def _dispatch_host(self, mod_id: str, method: str, params: dict) -> object:
        """处理 Node 的 $.xxx 反向调用。未知方法抛错（Node 侧收到 error）。"""
        if method == "store.get":
            return self._read_store(mod_id).get(str(params.get("key") or ""))
        if method == "store.set":
            data = self._read_store(mod_id)
            data[str(params.get("key") or "")] = params.get("value")
            self._write_store(mod_id, data)
            return None
        if method == "store.all":
            return self._read_store(mod_id)
        if method == "store.delete":
            data = self._read_store(mod_id)
            data.pop(str(params.get("key") or ""), None)
            self._write_store(mod_id, data)
            return None

        if method == "settings.get":
            getter = self.settings_getter
            if getter is None:
                return None
            settings = getter() or {}
            return settings.get(str(params.get("key") or ""))

        if method in ("model.complete", "model.fork"):
            return await self._model_call(params)

        if method == "mcp.list":
            mgr = self.mcp_manager
            if mgr is None:
                return []
            return [
                {"name": name, "enabled": bool(getattr(st, "enabled", False))}
                for name, st in (getattr(mgr, "statuses", {}) or {}).items()
            ]
        if method == "mcp.call":
            mgr = self.mcp_manager
            if mgr is None:
                raise RuntimeError("MCP 管理器未注入")
            return await self._mcp_call(
                mgr, str(params.get("server") or ""),
                str(params.get("tool") or ""), params.get("args") or {},
            )

        if method in ("tool.register", "command.register"):
            mod = self.mods.get(mod_id)
            if mod is None:
                raise RuntimeError(f"未找到 Mod：{mod_id}")
            spec = dict(params or {})
            name = str(spec.get("name") or "").strip()
            if not name:
                raise RuntimeError("register 需要 name")
            bucket = mod.registered_tools if method == "tool.register" else mod.registered_commands
            # 同名覆盖，与 Node 侧 register 语义一致
            bucket[:] = [s for s in bucket if s.get("name") != name]
            bucket.append(spec)
            logger.info("[mods:%s] 注册%s：%s", mod_id,
                        "工具" if method == "tool.register" else "命令", name)
            if method == "tool.register" and self.on_tools_changed is not None:
                try:
                    self.on_tools_changed()
                except Exception:  # noqa: BLE001 - 回调失败不影响注册本身
                    logger.warning("[mods:%s] on_tools_changed 回调失败", mod_id)
            return None

        raise RuntimeError(f"未知 host 方法：{method}")

    async def _model_call(self, params: dict) -> dict:
        """用当前 provider 发起一次非流式补全（P4：只取文本，忽略工具调用）。"""
        getter = self.provider_getter
        provider = getter() if getter is not None else None
        if provider is None:
            raise RuntimeError("provider 未注入")
        prompt = str(params.get("prompt") or "")
        system = str(params.get("system") or "")
        if not prompt:
            raise RuntimeError("model.complete 需要 prompt")
        msgs: list = []
        if system:
            msgs.append(Message.user(system))
        msgs.append(Message.user(prompt))
        texts: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        try:
            async for ev in provider.stream(msgs, []):
                if isinstance(ev, ProviderTextDelta):
                    texts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    usage = {
                        "input_tokens": ev.input_tokens,
                        "output_tokens": ev.output_tokens,
                    }
        except Exception as e:  # noqa: BLE001 - 上游错误回给 Mod，不炸宿主
            raise RuntimeError(f"模型调用失败：{e}") from e
        return {"text": "".join(texts), "usage": usage}

    async def _mcp_call(self, mgr, server: str, tool: str, args: dict) -> object:
        session = mgr.session_for(server)
        if session is None:
            raise RuntimeError(f"MCP 服务器未连接：{server}")
        result = await session.call_tool(tool, arguments=args)
        # 把 MCP 结果压成简单结构（content 列表 → 文本拼接 + raw）
        parts: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        return {
            "is_error": bool(getattr(result, "isError", False)),
            "text": "\n".join(parts),
        }

    def has_active(self) -> bool:
        """是否有启用中的 Mod（agent 用来决定能否走并发批）。"""
        return bool(self._active())

    def id_active(self, mod_id: str) -> bool:
        """该 Mod 是否被启用且总开关开着（与 quickjs ModManager.id_active 同形）。"""
        return self.enabled and mod_id in self.enabled_ids

    def reload_config(self, *, enabled: bool, enabled_ids: list[str]) -> None:
        self.enabled = enabled
        self.enabled_ids = list(enabled_ids)

    async def close(self) -> None:
        for mod in self.mods.values():
            await mod.close()

    # ---- 事件 ----

    async def on_tool_check(
        self, tool_name: str, input_dict: dict, session_id: str,
    ) -> dict:
        """权限决策：合并所有 Mod 的 verdict（deny > ask > allow）。

        rewrite（input 出现）从第一个给出 rewrite 的 Mod 开始生效，
        后续 Mod 看到的是改写后的 input。
        """
        errors: list[str] = []
        decision = None
        reason = ""
        current_input = dict(input_dict)
        for mod in self._active():
            payload = {
                "tool": tool_name,
                "input": current_input,
                "sessionId": session_id,
            }
            verdict, err = await mod.run("tool.check", payload, session_id)
            if err:
                errors.append(f"{mod.id}: {err}")
                continue
            if not isinstance(verdict, dict):
                continue
            v_decision = str(verdict.get("decision") or "").lower()
            # reason 无条件捕获：Mod 可能只回 reason（用于日志/说明），
            # 或回 reason 但 decision 未设置——都该让调用方看到
            v_reason = str(verdict.get("reason") or "")
            if v_reason and not reason:
                reason = v_reason
            if v_decision in (DECISION_DENY,):
                decision = DECISION_DENY
                reason = str(verdict.get("reason") or f"被 Mod「{mod.name}」拒绝")
                break  # deny 直接终止
            if v_decision == DECISION_ASK:
                if decision != DECISION_DENY:
                    decision = DECISION_ASK
                    if not reason:
                        reason = str(verdict.get("reason") or "")
            if v_decision == DECISION_ALLOW and decision is None:
                decision = DECISION_ALLOW
                if not reason:
                    reason = str(verdict.get("reason") or "")
            if isinstance(verdict.get("input"), dict):
                current_input = dict(verdict["input"])
        return {
            "decision": decision,
            "reason": reason,
            "input": current_input if current_input != input_dict else None,
            "errors": errors,
        }

    async def on_action(
        self, mod_id: str, action_id: str, kind: str, value: object, session_id: str,
    ) -> list[dict]:
        """UI 交互回传：只投给发起它的那个 Mod（不是全部）。

        Mod 用 register('ui.action', ($, e, next) => ...) 接收：
          e.payload = { actionId, kind, value }
        返回的 verdict.ui 会作为新的元素树刷新对应区域。
        """
        mod = self.mods.get(mod_id)
        if mod is None or mod.id not in self.enabled_ids:
            return [{"mod_id": mod_id, "error": f"Mod 未启用或不存在：{mod_id}"}]
        payload = {"actionId": action_id, "kind": kind, "value": value}
        verdict, err = await mod.run("ui.action", payload, session_id)
        if err:
            return [{"mod_id": mod_id, "error": err}]
        if verdict is None:
            return []
        entry: dict = {"mod_id": mod_id, "verdict": verdict}
        if isinstance(verdict, dict) and verdict.get("ui") is not None:
            slot = str(verdict.get("slot") or "stream").strip()
            if slot not in ("pane", "above_prompt", "stream", "tray", "perm", "dialog"):
                slot = "stream"
            tree = normalize_ui_tree(verdict.get("ui"))
            if tree is None:
                entry["error"] = "ui 元素树不合法，已丢弃"
            else:
                entry["ui"] = {"slot": slot, "tree": tree}
        return [entry]

    async def on_event(self, event: str, payload: dict, session_id: str) -> list[dict]:
        """通用事件分发：返回各 Mod 的 verdict（供调用方合并）。

        verdict 里的 ui 字段（元素树）会被校验归一：不合法整棵丢弃并记错误，
        合法时挂在结果的 ui 键上，供调用方产出 ModUI 事件。
        """
        out: list[dict] = []
        for mod in self._active():
            verdict, err = await mod.run(event, payload, session_id)
            if err:
                out.append({"mod_id": mod.id, "error": err})
                continue
            if verdict is None:
                continue
            entry: dict = {"mod_id": mod.id, "verdict": verdict}
            if isinstance(verdict, dict) and verdict.get("ui") is not None:
                slot = str(verdict.get("slot") or "stream").strip()
                if slot not in ("pane", "above_prompt", "stream", "tray", "perm", "dialog"):
                    slot = "stream"
                tree = normalize_ui_tree(verdict.get("ui"))
                if tree is None:
                    entry["error"] = "ui 元素树不合法，已丢弃"
                else:
                    entry["ui"] = {"slot": slot, "tree": tree}
            out.append(entry)
        return out


# ---- 兼容判断 ----

def is_claude_mode(mods: object) -> bool:
    """检查给定的 mods 对象是否是 Claude Code 兼容模式。"""
    return isinstance(mods, ClaudeModManager)
