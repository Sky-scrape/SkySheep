"""CLI 渲染器：把 AgentEvent 流变成终端输出。

文本增量用 rich.Live 实时刷新；流结束后以 Markdown 打印完整回复；
工具调用打印一行摘要 + 结果预览；敏感操作打印高亮确认面板。
"""

from __future__ import annotations

import re

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from ..core.estimate import format_range
from ..events import AgentEvent

# 终端控制序列剥离（安全审查 M14）：模型输出、工具结果、命令输出里可能嵌
# ANSI 转义——\x1b[2J 清屏、\x1b]0;标题 改窗口标题、\x1b[?25l 隐藏光标。
# rich 的 strip_control_codes 不剥 ESC，这些序列会被原样打到终端（已实测复现）。
# 这些内容都来自不可信输入，渲染前必须剥掉；只影响 CLI 面（GUI 是浏览器渲染，
# 转义序列本就惰性）。
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")            # CSI：光标/清屏/颜色
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")  # OSC：改标题
_ANSI_ESC_RE = re.compile(r"\x1b.")  # 双字符转义（ESC 7/8 存游标这类）
_ANSI_C1_RE = re.compile(r"\x9b[0-?]*[ -/]*[@-~]")  # C1 CSI（单字节 0x9b 起）
# 字符集/编码选择：ESC ( B、ESC % G 这类三字符序列（先于通用双字符规则匹配）
_ANSI_CHARSET_RE = re.compile(r"\x1b[()*+#%][^\x1b]?")
# 其余控制符：C0（保留 \n \t）与 C1（含 DEL）。残留的孤立 ESC 也在这里被清掉
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_terminal_text(text: str) -> str:
    """剥掉终端控制序列与不可打印控制符，保留换行与制表符。"""
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_CHARSET_RE.sub("", text)
    text = _ANSI_ESC_RE.sub("", text)
    text = _ANSI_C1_RE.sub("", text)
    return _CONTROL_RE.sub("", text)


def _message_text(message_dict: dict) -> str:
    return "".join(
        b.get("text", "") for b in message_dict.get("content", []) if b.get("type") == "text"
    )


class Renderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._live: Live | None = None
        self._buf: list[str] = []
        self._think: list[str] = []

    # ---- 文本流 ----

    def _ensure_live(self, text: Text | None = None) -> None:
        if self._live is None:
            # transient=True：停止时擦除流式预览，随后只打印最终 Markdown，避免重复
            self._live = Live(
                text or Text("".join(self._buf)),
                console=self.console,
                refresh_per_second=24,
                transient=True,
            )
            self._live.start()
        else:
            self._live.update(text or Text("".join(self._buf)))

    def _flush_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    # ---- 事件分发 ----

    def handle(self, ev: AgentEvent) -> None:
        k = ev.kind
        if k == "thinking_delta":
            # 思考先于正文：实时灰色展示（transient，结束时擦除，不与正文混淆）
            self._think.append(sanitize_terminal_text(ev.text))
            self._buf = []
            self._ensure_live(Text("".join(self._think), style="dim"))
        elif k == "text_delta":
            if self._think:
                self._think = []
            self._buf.append(sanitize_terminal_text(ev.text))
            self._ensure_live()
        elif k == "assistant_message":
            self._flush_live()
            if self._think:
                self.console.print(Text(f"💭 思考过程（{len(''.join(self._think))} 字）", style="dim"))
                self._think = []
            text = sanitize_terminal_text(_message_text(ev.message))
            if text.strip():
                self.console.print(Markdown(text))
                self.console.print()
        elif k == "turn_started":
            self._buf = []
            self._think = []
        elif k == "tool_call_started":
            self._flush_live()
            args = _one_line(ev.input)
            self.console.print(
                Text.assemble(("▶ ", "cyan"), (ev.name, "cyan bold"), (" " + args, "cyan dim"))
            )
        elif k == "tool_call_finished":
            flag = "✗" if ev.is_error else "✓"
            style = "red" if ev.is_error else "green"
            head = f"  {flag} {ev.name} ({ev.duration_ms}ms)"
            self.console.print(Text(head, style=style))
            if ev.preview:
                for line in sanitize_terminal_text(ev.preview).splitlines()[:8]:
                    self.console.print(Text("    " + line, style="dim"))
                more = ev.preview.splitlines()
                if len(more) > 8:
                    self.console.print(Text(f"    ... ({len(more) - 8} more lines)", style="dim"))
        elif k == "permission_request":
            self._flush_live()
            args = _one_line(ev.input)
            body = args if len(args) <= 2000 else args[:2000] + " ..."
            if ev.note:
                body += "\n" + sanitize_terminal_text(ev.note)
            if ev.rule_kind:
                kind_cn = {
                    "always": "整个工具",
                    "prefix": "前缀",
                    "exact": "仅此一条",
                    "glob": "通配",
                }.get(ev.rule_kind, ev.rule_kind)
                pat = sanitize_terminal_text(ev.rule_pattern) or "（全部）"
                body += f"\n「总是允许」将添加规则：{ev.tool_name} · {kind_cn} {pat}"
            self.console.print(
                Panel(
                    Text(body, style="yellow"),
                    title="🔒 需要确认: " + ev.tool_name + " [" + ev.safety + "]",
                    border_style="yellow",
                )
            )
        elif k == "permission_resolved":
            label = {
                "allow_once": "本次允许",
                "allow_always": "本项目总是允许",
                "deny": "已拒绝",
            }.get(ev.decision, ev.decision)
            self.console.print(Text("  → " + label, style="dim"))
        elif k == "notice":
            self._flush_live()
            self.console.print(
                Text("⏳ " + sanitize_terminal_text(ev.message), style="yellow dim")
            )
        elif k == "error":
            self._flush_live()
            self.console.print(
                Text("✗ " + sanitize_terminal_text(ev.message), style="bold red")
            )
        elif k == "compaction":
            self._flush_live()
            msg = (
                f"🗜 上下文压缩: {ev.before_messages} → {ev.after_messages} 条消息"
                f"（摘要 {ev.summary_chars} 字符）"
            )
            self.console.print(Text(msg, style="cyan dim"))
        elif k == "task_estimate":
            self._flush_live()
            line = f"⏱ 预计耗时 {format_range(ev.min_seconds, ev.max_seconds)}"
            if ev.basis:
                line += f"（{ev.basis}）"
            self.console.print(Text(line, style="dim"))
        elif k == "turn_finished":
            self._flush_live()

    def close(self) -> None:
        self._flush_live()


def _one_line(d: dict, limit: int = 160) -> str:
    parts = []
    for key, value in d.items():
        s = sanitize_terminal_text(str(value))
        if len(s) > limit:
            s = s[:limit] + "..."
        parts.append(f"{key}={s}")
    return " ".join(parts)[:400]
