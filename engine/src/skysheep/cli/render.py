"""CLI 渲染器：把 AgentEvent 流变成终端输出。

文本增量用 rich.Live 实时刷新；流结束后以 Markdown 打印完整回复；
工具调用打印一行摘要 + 结果预览；敏感操作打印高亮确认面板。
"""

from __future__ import annotations

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from ..events import AgentEvent


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
            self._think.append(ev.text)
            self._buf = []
            self._ensure_live(Text("".join(self._think), style="dim"))
        elif k == "text_delta":
            if self._think:
                self._think = []
            self._buf.append(ev.text)
            self._ensure_live()
        elif k == "assistant_message":
            self._flush_live()
            if self._think:
                self.console.print(Text(f"💭 思考过程（{len(''.join(self._think))} 字）", style="dim"))
                self._think = []
            text = _message_text(ev.message)
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
                for line in ev.preview.splitlines()[:8]:
                    self.console.print(Text("    " + line, style="dim"))
                more = ev.preview.splitlines()
                if len(more) > 8:
                    self.console.print(Text(f"    ... ({len(more) - 8} more lines)", style="dim"))
        elif k == "permission_request":
            self._flush_live()
            args = _one_line(ev.input)
            self.console.print(
                Panel(
                    Text(args if len(args) <= 2000 else args[:2000] + " ...", style="yellow"),
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
            self.console.print(Text("⏳ " + ev.message, style="yellow dim"))
        elif k == "error":
            self._flush_live()
            self.console.print(Text("✗ " + ev.message, style="bold red"))
        elif k == "compaction":
            self._flush_live()
            msg = (
                f"🗜 上下文压缩: {ev.before_messages} → {ev.after_messages} 条消息"
                f"（摘要 {ev.summary_chars} 字符）"
            )
            self.console.print(Text(msg, style="cyan dim"))
        elif k == "turn_finished":
            self._flush_live()

    def close(self) -> None:
        self._flush_live()


def _one_line(d: dict, limit: int = 160) -> str:
    parts = []
    for key, value in d.items():
        s = str(value)
        if len(s) > limit:
            s = s[:limit] + "..."
        parts.append(f"{key}={s}")
    return " ".join(parts)[:400]
