"""Permission Gate：敏感操作确认与白名单。

工具分级：
- READONLY：自动放行；
- WRITE / DANGEROUS：需用户确认；用户可选择"本项目永久允许"，
  由会话存储记录规则，之后 authorize 直接放行。

与 Agent 循环的交互协议：
1. authorize() 先查白名单；命中即放行；
2. 未命中返回 PendingPermission（含 request_id），循环把它包装成
   PermissionRequest 事件抛给前端，然后 await 其 future；
3. 用户在 UI/CLI 上决策后调用 pending.resolve(decision)；
4. decision=allow_always 时写入一条项目级白名单规则。
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..tools.base import Safety, Tool

if TYPE_CHECKING:
    from ..session.store import SessionStore


class Decision:
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY = "deny"


@dataclass
class WhitelistRule:
    tool: str
    kind: str  # "always" | "prefix" | "glob"
    pattern: str = ""

    def matches(self, tool_name: str, arg_text: str) -> bool:
        if self.tool != tool_name:
            return False
        if self.kind == "always":
            return True
        if self.kind == "prefix":
            return arg_text.startswith(self.pattern)
        if self.kind == "glob":
            return fnmatch.fnmatch(arg_text, self.pattern)
        return False


@dataclass
class PendingPermission:
    request_id: str
    tool_name: str
    arg_text: str
    safety: Safety
    detail: str
    _future: asyncio.Future = field(repr=False)
    diff: str = ""  # 写入类工具的改前→改后 unified diff 预览（确认时有真实依据）

    def resolve(self, decision: str) -> None:
        if not self._future.done():
            self._future.set_result(decision)

    async def wait(self) -> str:
        return await self._future


class PermissionGate:
    """每次会话创建一个实例；持有项目/会话白名单规则。"""

    def __init__(
        self,
        store: SessionStore | None = None,
        project_id: int | None = None,
        session_rules: list[WhitelistRule] | None = None,
        working_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.session_rules: list[WhitelistRule] = list(session_rules or [])
        self.working_dir = Path(working_dir).resolve() if working_dir else None
        # 分级权限模式（对标 Codex Auto-Edit / Claude Code acceptEdits）：
        # True 时「写入」类工具（write_file/edit_file/generate_image 等）自动放行，
        # 「高危」（run_command 等）仍走确认——沙箱缺失，不做全自动档。
        self.auto_accept_write: bool = False
        self._project_rules: list[WhitelistRule] = []
        self.on_request: Callable[[PendingPermission], Awaitable[None]] | None = None

    async def load_project_rules(self) -> None:
        if self.store and self.project_id is not None:
            rows = await self.store.list_rules(self.project_id)
            self._project_rules = [
                WhitelistRule(tool=r["tool"], kind=r["kind"], pattern=r["pattern"]) for r in rows
            ]

    def add_session_rule(self, rule: WhitelistRule) -> None:
        self.session_rules.append(rule)

    def _match(self, tool: Tool, arg_text: str) -> bool:
        rules = self.session_rules + self._project_rules
        return any(r.matches(tool.name, arg_text) for r in rules)

    # 电脑控制类工具：arg_text 首词是动作（click / type / hotkey / activate …），
    # 白名单按动作前缀生成，粒度到动作级（如只放行 click、只放行 activate）
    _ACTION_PREFIX_TOOLS = ("mouse", "keyboard", "window")

    @staticmethod
    def rule_for(tool: Tool, input_dict: dict) -> WhitelistRule:
        """根据本次调用生成"永久允许"规则（run_command 用命令前缀，其余整工具放行）。"""
        arg_text = tool.arg_text(input_dict)
        if tool.name == "run_command":
            # 取前两个词作为前缀，如 "git status" / "npm run"，避免把整条命令固化
            words = arg_text.split()
            prefix = " ".join(words[:2]) if words else arg_text
            return WhitelistRule(tool=tool.name, kind="prefix", pattern=prefix)
        if tool.name in PermissionGate._ACTION_PREFIX_TOOLS:
            words = arg_text.split()
            return WhitelistRule(
                tool=tool.name, kind="prefix", pattern=words[0] if words else arg_text
            )
        return WhitelistRule(tool=tool.name, kind="always")

    async def authorize(self, tool: Tool, input_dict: dict) -> PendingPermission | None:
        """返回 None 表示放行；返回 PendingPermission 表示需要用户决策。"""
        if tool.safety == Safety.READONLY:
            return None
        # 自动允许写入档：只放行 WRITE 级，高危操作（执行命令）仍需确认
        if self.auto_accept_write and tool.safety == Safety.WRITE:
            return None
        arg_text = tool.arg_text(input_dict)
        if self._match(tool, arg_text):
            return None
        pending = PendingPermission(
            request_id=uuid.uuid4().hex[:12],
            tool_name=tool.name,
            arg_text=arg_text,
            safety=tool.safety,
            detail=arg_text,
            _future=asyncio.get_running_loop().create_future(),
            diff=self._preview_diff(tool, input_dict),
        )
        if self.on_request:
            await self.on_request(pending)
        return pending

    # 写入类工具的改前→改后预览；仅限工作目录内的文本文件，失败静默降级为无 diff
    DIFF_PREVIEW_MAX_LINES = 160

    def _preview_diff(self, tool: Tool, input_dict: dict) -> str:
        if not self.working_dir or tool.name not in ("write_file", "edit_file", "write_document"):
            return ""
        raw = str(input_dict.get("path", "") or "")
        if not raw:
            return ""
        target = Path(raw)
        if not target.is_absolute():
            target = self.working_dir / target
        try:
            target.resolve().relative_to(self.working_dir)
        except ValueError:
            return ""  # 工作目录之外：不给预览（工具层还会拦）
        try:
            old = target.read_text(encoding="utf-8") if target.is_file() else ""
        except (OSError, UnicodeDecodeError):
            return ""
        if tool.name == "write_file":
            new = str(input_dict.get("content", "") or "")
        elif tool.name == "write_document":
            # 确认弹窗展示源内容（Markdown/JSON）预览；.csv 是文本可做真实 diff，
            # 覆盖已有 docx/xlsx 时旧内容是二进制，给不出有意义的文本 diff
            if target.exists() and target.suffix.lower() != ".csv":
                return ""
            try:
                old = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
            except (OSError, UnicodeDecodeError):
                return ""
            new = str(input_dict.get("content", "") or "")
        else:  # edit_file：按工具语义模拟替换
            old_s = input_dict.get("old_string")
            new_s = input_dict.get("new_string")
            if not isinstance(old_s, str) or not isinstance(new_s, str) or old_s not in old:
                return ""
            count = -1 if input_dict.get("replace_all") else 1
            new = old.replace(old_s, new_s, count)
        if old == new:
            return ""
        lines = list(
            difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile="改前", tofile="改后", lineterm="",
            )
        )
        if len(lines) > self.DIFF_PREVIEW_MAX_LINES:
            rest = len(lines) - self.DIFF_PREVIEW_MAX_LINES
            lines = lines[: self.DIFF_PREVIEW_MAX_LINES] + [f"…（余 {rest} 行省略）"]
        return "\n".join(lines)

    async def persist_rule(self, rule: WhitelistRule) -> None:
        """把"永久允许"规则写入项目存储并立即生效。"""
        self._project_rules.append(rule)
        if self.store and self.project_id is not None:
            await self.store.add_rule(
                project_id=self.project_id,
                tool=rule.tool,
                kind=rule.kind,
                pattern=rule.pattern,
            )


class HeadlessGate(PermissionGate):
    """无人值守门控（定时任务 / headless run 共用）：只读自动放行；创建时
    预授权名单内的工具自动放行；其余写/高危操作自动拒绝——Agent 收到
    「已拒绝」后继续，不阻塞也不弹窗。"""

    def __init__(self, allowed: list[str], **kw) -> None:
        super().__init__(**kw)
        self.allowed = set(allowed or [])

    async def authorize(self, tool: Tool, input_dict: dict) -> PendingPermission | None:
        if tool.safety == Safety.READONLY:
            return None
        if tool.name in self.allowed:
            return None
        pending = await super().authorize(tool, input_dict)
        if pending is not None:
            pending.resolve(Decision.DENY)
        return pending
