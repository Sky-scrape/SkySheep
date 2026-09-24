"""memory_write 工具：Agent 的用户级全局记忆（对标 Claude Code / ZCode 的 memory）。

AGENTS.md 是项目级、手动编辑的项目约定；本工具补的是「跨项目的用户记忆」：
Agent 在对话中学到值得长期记住的事实（用户偏好、常用环境、背景信息）时自己
写入 ~/.skysheep/memory.md，系统提示词每一轮都注入该文件（限长）。

安全边界：只能写 SkySheep 自己的记忆文件（路径固定、不接受任何路径参数），
与 schedule_write 写应用自有存储同理，READONLY 免确认。

本模块还承载「归档自动记忆」与「定期自动整理」的纯函数部分（backend 调用）：
digest_transcript / build_digest_prompt / parse_digest 负责归档提炼，
maintenance_* / build_maintain_prompt / clean_maintained_text 负责定期整理，
remember_lines 负责落盘，判定标准与 memory_write 保持一致。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..textio import write_text_atomic
from .base import Safety, Tool, ToolContext, ToolError

MemoryAction = Literal["append", "list", "delete"]

MAX_MEMORY_CHARS = 4000          # 注入系统提示词的上限
MAX_MEMORY_FILE_CHARS = 200_000  # 文件本身的上限（防无限膨胀）

# ---- 归档自动提炼（判定标准与 memory_write 一致：偏好/环境/背景，勿记任务细节与机密） ----

DIGEST_MIN_CHARS = 300                # 会话正文低于此长度视为寒暄，不提炼
DIGEST_MAX_TRANSCRIPT_CHARS = 12_000  # 送入模型的会话正文上限（超出丢最旧）
DIGEST_MAX_ENTRIES = 8                # 单次最多提炼条数
DIGEST_ENTRY_MAX_CHARS = 100          # 单条记忆长度上限
_DIGEST_PLACEHOLDERS = {"无", "没有", "（无）", "(none)", "none", "n/a"}
# 「没有值得记录的内容」类占位句（剥标点后整体匹配，限长防误伤真条目）
_DIGEST_NOTHING_RE = re.compile(r"^(没有?|无需|不必|无可|没什么).{0,16}(记录|记住|提炼|值得|保存)")

# ---- 定期自动整理（全局 memory.md 与项目 AGENTS.md 的周期性合并去重） ----

MAINTAIN_MIN_GLOBAL_CHARS = 400    # 全局记忆短于此不整理（没东西可合并）
MAINTAIN_MIN_PROJECT_CHARS = 600   # 项目记忆是手写约定，更短时不值得动
MAINTENANCE_STATE_FILE = "memory-maintenance.json"  # ~/.skysheep/ 下的上次整理时间
MAINTENANCE_BACKUP_KEEP = 5        # 整理备份保留份数：带时间戳滚动，防单份 .bak 被下次覆盖


def memory_path() -> Path:
    from ..config import skysheep_home

    return skysheep_home() / "memory.md"


def inject_text(text: str) -> str:
    """系统提示词实际注入的记忆文本：超上限保最新的尾部、按行边界截断。

    条目按时间追加（新条目在文件尾部），越新越可能仍然有效——超限丢头不丢尾，
    与 _write_lines 的容量护栏（丢最旧条目）同一方向。半条记忆对模型是噪声，
    截断必须落在行边界；单行超长时退化为硬截该行（不会超出上限）。
    """
    if len(text) <= MAX_MEMORY_CHARS:
        return text
    return text[-MAX_MEMORY_CHARS:].split("\n", 1)[-1]


def load_memory_text() -> str:
    """读取记忆文本（超长按行边界截断）；没有文件返回空串。"""
    p = memory_path()
    try:
        return inject_text(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def render_memory_section() -> str:
    """系统提示词的记忆段落；无记忆时返回空串。

    发生截断时给模型一行说明：它看到的不是全部，更早的条目可用 memory_write
    的 list 动作查看——静默截断会让模型把「没注入」当成「不存在」。
    """
    p = memory_path()
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = inject_text(raw).strip()
    if not text:
        return ""
    if len(raw) > MAX_MEMORY_CHARS:
        text = (
            "（记忆条数超出单轮注入上限，以下只是最近的条目；"
            "更早的可用 memory_write 的 list 动作查看）\n" + text
        )
    return f"\n# User memory（跨项目的用户记忆，管理用 memory_write）\n{text}\n"


def digest_transcript(messages) -> str:
    """把会话消息压成「用户/助手：正文」的提炼稿；正文总量太短返回空串。

    messages 是 skysheep.messages.Message 列表（鸭子类型：只要有 role/text）。
    每条消息截前 800 字；总量超出预算时丢最旧的（近期上下文对提炼最有用）。
    """
    lines: list[str] = []
    total = 0
    for m in messages:
        if getattr(m, "role", "") not in ("user", "assistant"):
            continue
        text = (m.text or "").strip()
        if not text:
            continue
        total += len(text)
        lines.append(f"{'用户' if m.role == 'user' else '助手'}：{text[:800]}")
    if total < DIGEST_MIN_CHARS:
        return ""
    while lines and sum(len(ln) + 2 for ln in lines) > DIGEST_MAX_TRANSCRIPT_CHARS:
        lines.pop(0)
    return "\n\n".join(lines)


def build_digest_prompt(transcript: str) -> str:
    return (
        "下面是一段已结束的助手会话记录。请站在「长期为这位用户服务」的角度，"
        "提炼值得跨项目长期记住的信息（用户的偏好、习惯、环境、背景），"
        "例如常用工具链、目录位置、交付形式偏好、固定的做事约定。\n"
        "要求：\n"
        f"- 每条一行、以「- 」开头，最多 {DIGEST_MAX_ENTRIES} 条；没有值得记的就只输出：无\n"
        f"- 每条不超过 {DIGEST_ENTRY_MAX_CHARS} 字，具体、可长期有效、可独立理解\n"
        "- 一次性的任务细节（改了哪个文件、报了什么错）不要记\n"
        "- 密码、API Key 等机密信息绝对不要记\n\n"
        "会话记录：\n" + transcript
    )


def parse_digest(raw: str) -> list[str]:
    """解析模型输出的提炼稿：剥列表符号/编号，滤空行、占位行与前导语，批内去重。"""
    out: list[str] = []
    for ln in raw.splitlines():
        s = re.sub(r"^\d+[.、)）]\s*", "", re.sub(r"^[-*·•]\s*", "", ln.strip())).strip()
        if not s:
            continue
        # 占位行常带句尾标点（「无。」「没有值得记录的内容。」），剥掉再比对；
        # 剥完为空的纯标点行同样丢弃
        probe = s.rstrip("。．.!！?？；;，,、~～")
        if not probe or probe.lower() in _DIGEST_PLACEHOLDERS or (
            len(probe) <= 24 and _DIGEST_NOTHING_RE.search(probe)
        ):
            continue
        # 以冒号收尾的是「以下是提炼结果：」类前导语/标题，不是可独立理解的条目
        if s.endswith(("：", ":")):
            continue
        if len(s) > DIGEST_ENTRY_MAX_CHARS:
            s = s[:DIGEST_ENTRY_MAX_CHARS].rstrip() + "…"
        if s not in out:
            out.append(s)
        if len(out) >= DIGEST_MAX_ENTRIES:
            break
    return out


def _read_lines(p: Path) -> list[str]:
    # memory.md 是引擎自产文件（本模块恒以 UTF-8 落盘），不走 textio 的编码探测——
    # textio 面向的是用户手写的未知编码文件；这里用 errors="replace" 只作兜底
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _write_lines(p: Path, lines: list[str]) -> None:
    # 容量护栏：超限从最旧的条目开始丢
    while lines and sum(len(ln) + 1 for ln in lines) > MAX_MEMORY_FILE_CHARS:
        lines.pop(0)
    try:
        # 原子写：memory.md 是引擎自有状态文件，写一半被杀会留下半截记忆
        # 且它没有备份可回——整份记忆就这一次写入机会
        write_text_atomic(p, "\n".join(lines) + ("\n" if lines else ""))
    except OSError as e:
        raise ToolError(f"cannot write memory file: {e}") from e


def remember_lines(lines: list[str]) -> list[str]:
    """批量追加记忆条目（自动加日期前缀与「(自动)」来源标记），返回真正新增的内容。

    与 memory_write append 同一去重规则：内容已是现有条目的子串则跳过；
    容量护栏（MAX_MEMORY_FILE_CHARS）由 _write_lines 统一兜底。
    「(自动)」标记：归档提炼是无确认的后台写入，用户在全局记忆页要能
    一眼分辨并清理（手动 memory_write 的条目不带标记）。
    """
    p = memory_path()
    existing = _read_lines(p)
    added: list[str] = []
    today = date.today().isoformat()
    for content in lines:
        content = content.strip()
        if not content or any(content in ln for ln in existing):
            continue
        existing.append(f"- [{today}] (自动) {content}")
        added.append(content)
    if added:
        _write_lines(p, existing)
    return added


# ---- 定期自动整理：状态、到期判断、提示词与输出清洗（backend 调用的纯函数） ----


def maintenance_state_path() -> Path:
    from ..config import skysheep_home

    return skysheep_home() / MAINTENANCE_STATE_FILE


def load_maintenance_state() -> dict:
    """上次整理时间（{"global_last": ts, "project_last": {工作目录: ts}}）；损坏按空处理。"""
    try:
        raw = maintenance_state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_maintenance_state(state: dict) -> None:
    try:
        write_text_atomic(maintenance_state_path(), json.dumps(state, ensure_ascii=False))
    except OSError:
        pass  # 状态写不进去只影响下次提前整理，不值得打断主流程


def backup_before_maintain(path: Path, old_text: str) -> Path:
    """整理前把原件备份为带时间戳的 .bak-YYYYMMDD-HHMMSS-微秒，滚动保留最近数份。

    备份写失败直接抛 OSError——调用方应放弃本次覆盖（宁可不整理也不裸写）；
    旧备份清理失败只影响磁盘占用，不跟着失败。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    # Windows 的 datetime.now() 在部分机器（CI 虚拟机常见）精度只有 ~15.6ms，
    # 连续两次备份可能拿到同一个时间戳，按名直接写会把上一份盖掉。重名时追加
    # 零填充序号：字典序仍落在原时间戳之后，滚动清理的「按名排序取最新」不变。
    n = 0
    while bak.exists():
        n += 1
        bak = path.with_name(f"{path.name}.bak-{stamp}-{n:03d}")
    # 备份也走原子写：备份是原件的唯一恢复途径，半截备份等于没有备份
    write_text_atomic(bak, old_text)
    try:
        baks = sorted(path.parent.glob(path.name + ".bak-*"))
        for extra in baks[:-MAINTENANCE_BACKUP_KEEP]:
            extra.unlink(missing_ok=True)
    except OSError:
        pass
    return bak


def maintenance_due(
    state: dict, *, global_enabled: bool, project_enabled: bool,
    interval_hours: int, workdir: str, now: float,
) -> tuple[bool, bool]:
    """返回（全局是否到期, 项目是否到期）。没整理过（无记录）视为早已到期。"""
    p_last = float((state.get("project_last") or {}).get(workdir) or 0)
    g_last = float(state.get("global_last") or 0)
    span = max(1, int(interval_hours)) * 3600
    return (
        bool(global_enabled) and now - g_last >= span,
        bool(project_enabled) and now - p_last >= span,
    )


def build_maintain_prompt(scope: str, text: str) -> str:
    if scope == "global":
        return (
            "下面是一份跨项目的用户长期记忆（每条一行，格式「- [日期] 内容」）。"
            "请整理这份列表：合并重复与意思相近的条目（日期保留更早的那条）、"
            "删除明显过时或相互矛盾的条目里过时的那条、精简啰嗦的表述，"
            "不要添加列表之外的新信息，不要改变「- [日期] 内容」的格式。\n"
            "只输出整理后的完整列表，不要任何解释或代码块标记。\n\n" + text
        )
    return (
        "下面是一份项目约定文件（AGENTS.md），每一轮对话都会注入给助手。"
        "请整理这份文件：保留所有仍然有效的约定与原有结构，合并重复表述，"
        "删除明显过时、与其它条目矛盾的内容，不要添加文件之外的新约定，"
        "不要改变 Markdown 结构与小节标题。\n"
        "只输出整理后的完整文件内容，不要任何解释或代码块标记。\n\n" + text
    )


_LIST_LINE_RE = re.compile(r"\s*-\s")


def clean_maintained_text(raw: str, old_text: str, max_chars: int) -> str | None:
    """清洗模型输出的整理稿：剥外围空白与代码围栏；无效或与原文相同返回 None。

    max_chars 是落盘上限（全局/项目各自不同），超限视为模型输出失控，拒绝。
    另有结构守卫：原文是条目列表而输出里一条列表行都不剩，是模型把记忆写成了
    散文——行格式被破坏后 (自动) 标记、追加去重都失效，宁可不动原件。
    """
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.endswith("```"):
            s = s[: -3]
    s = s.strip()
    if not s or s == old_text.strip() or len(s) > max_chars:
        return None
    if any(_LIST_LINE_RE.match(ln) for ln in old_text.splitlines()) and not any(
        _LIST_LINE_RE.match(ln) for ln in s.splitlines()
    ):
        return None
    return s


class MemoryWriteArgs(BaseModel):
    action: MemoryAction = Field(description="append 追加一条 / list 查看全部 / delete 按内容删除")
    content: str = Field(default="", description="append：要记住的内容（一句话，具体、可长期有效）")
    match: str = Field(default="", description="delete：要删除条目里包含的原文片段")


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "管理你的跨项目用户记忆（用户偏好、常用环境、长期有效的事实）。"
        "当用户说「记住我喜欢…」「以后都用…」，或你发现值得长期记住的信息时用 append；"
        "用户要求忘记某事时用 delete。记忆会自动注入你之后的每一轮对话。"
    )
    safety = Safety.READONLY  # 只写 SkySheep 自有记忆文件，不需要确认
    # 只写 SkySheep 自有记忆文件（safety=READONLY 免确认），但对环境有写动作
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = MemoryWriteArgs

    async def run(self, args: MemoryWriteArgs, ctx: ToolContext) -> str:
        p = memory_path()
        if args.action == "append":
            content = (args.content or "").strip()
            if not content:
                raise ToolError("append 需要 content（要记住的内容）")
            lines = _read_lines(p)
            if any(content in ln for ln in lines):
                return "already remembered（已有相同内容的记忆，不重复追加）"
            lines.append(f"- [{date.today().isoformat()}] {content}")
            _write_lines(p, lines)
            return f"remembered: {content[:80]}"
        if args.action == "list":
            lines = _read_lines(p)
            if not lines:
                return "(memory is empty)"
            return "\n".join(lines)
        # delete
        match = (args.match or "").strip()
        if not match:
            raise ToolError("delete 需要 match（要删除条目包含的片段）")
        lines = _read_lines(p)
        kept = [ln for ln in lines if match not in ln]
        removed = len(lines) - len(kept)
        if not removed:
            return f"no memory entry contains {match!r}"
        _write_lines(p, kept)
        return f"forgot {removed} entr{'y' if removed == 1 else 'ies'}"
