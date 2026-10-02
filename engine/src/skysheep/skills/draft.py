"""会话 → 技能草稿：把一次成功会话的做法确定性整理成 SKILL.md 草稿。

skills.save_from_session / skills.save_draft（server/backend.py）的生成与落盘层。
只做机械提取，**不调模型**：目标取首条用户消息，步骤取实际发生过的工具调用
（按轮次、只读与写入/执行分开列），写入/执行类操作同时列为注意事项。
生成结果先回前端编辑框（名称 / 描述 / 正文都可改），用户确认后才落盘。

后续若接 AI 润色：在 build_skill_draft 的产物上再包一层改写即可（输入契约是
消息列表 + 工具安全分级查询，输出契约是 name / description / 四小节正文），
本模块的提取逻辑原样保留作不调模型的兜底路径。

「注意事项」的口径：权限确认事件（PermissionRequest / PermissionResolved）只
存在于事件流、不落库，从持久化消息无法区分「弹窗确认过」与「白名单放行」——
能确定的只有写入/执行类工具（safety 非 readonly）默认都要过权限门。所以注意
事项按「本会话出现过的写入/执行类调用」列，附「需要用户确认」的通用提示；
这是保守且可复现的口径，不臆造每一条的实际确认方式。

「适用边界」的口径：技能可能被分享或跨项目复用，原会话的绝对路径一律把项目
根泛化成「<项目>」占位符——既不把本机目录结构写死进技能，也不把项目路径
泄露给拿到技能文件的人。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..messages import Message, ToolUseBlock
from ..textio import write_text_atomic
from .installer import SkillInstallError, _skill_target, _validate_skill_name
from .loader import _parse_frontmatter

# 技能正文上限，对齐 load_skill 的读取上限（tools/skill.py 的 MAX_SKILL_CHARS）：
# 超过它保存的技能永远只会被读到半截，生成时直接截断、保存时直接拒绝
MAX_DRAFT_BODY_CHARS = 20_000

# 建议名 / 描述 / 目标引用的截断长度。名字只是建议（编辑框可改），
# 合法形状由保存时的 _validate_skill_name 把关
_SUGGESTED_NAME_MAX = 40
_DESCRIPTION_MAX = 100
_GOAL_MAX = 500
# 步骤里单个参数值的展示截断（要点不是全量回放，完整值在会话记录里）
_ARG_VALUE_MAX = 60

# 生成建议名时直接剔掉的字符：Windows 目录名非法字符 + 路径分隔符与盘符冒号
# （后两者保存时由 _validate_skill_name 拒绝，建议名先剔掉省得用户白改一轮）
_NAME_BAD_CHARS = '<>"|?*/\\:'

_TURN_READ_ONLY = "只读"
_TURN_WRITE = "写入/执行"


def _generalize_path(text: str, project_root: Path | str | None) -> str:
    """把文本里出现的项目根路径泛化成「<项目>」占位符。

    正斜杠 / 反斜杠两种写法都替换；大小写按原样匹配（Windows 目录大小写
    不敏感，但这里追求的是「别把完整路径写进技能」，漏掉大小写变体只是
    少泛化一处，不会出错）。
    """
    if not project_root:
        return text
    root = str(project_root).rstrip("\\/")
    out = text
    for form in dict.fromkeys((root, root.replace("\\", "/"))):
        if form and form in out:
            out = out.replace(form, "<项目>")
    return out


def _first_line(text: str) -> str:
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def suggest_name(first_user_text: str) -> str:
    """从首条用户消息建议一个技能名（形状安全、可读、可改）。"""
    line = _first_line(first_user_text)
    # 去掉常见的列表 / 标题前缀符号，再剔掉目录名非法字符
    line = line.lstrip("#-*•>、 ")
    cleaned = "".join(ch for ch in line if ch not in _NAME_BAD_CHARS)
    cleaned = " ".join(cleaned.split())
    return cleaned[:_SUGGESTED_NAME_MAX].strip(" .") or "未命名技能"


def _arg_summary(tool_input: dict, project_root: Path | str | None) -> str:
    """一次工具调用的参数要点（单行；值超长截断，路径先泛化）。"""
    parts: list[str] = []
    for key, value in (tool_input or {}).items():
        if isinstance(value, (dict, list)):
            raw = json.dumps(value, ensure_ascii=False)
        else:
            raw = str(value)
        raw = _generalize_path(" ".join(raw.split()), project_root)
        if len(raw) > _ARG_VALUE_MAX:
            raw = raw[:_ARG_VALUE_MAX] + "…"
        parts.append(f"{key}={raw}")
    return ", ".join(parts)


def _call_key(name: str, summary: str) -> tuple[str, str]:
    return name, summary


def extract_turns(
    messages: list[Message],
    safety_of: Callable[[str], str] | None,
    project_root: Path | str | None,
) -> list[dict]:
    """按轮次提取实际发生过的工具调用。

    返回 [{turn, read_only: [(name, summary)], write: [(name, summary)]}]，
    只含真的有调用的轮次；同一轮里连续相同的调用折叠为一条（重试同参数
    是常见形态，逐条罗列只会稀释要点）。safety_of 查不到的名字（MCP 服务器
    未连接等）按写入/执行类保守处理。
    """
    turns: list[dict] = []
    current: dict | None = None
    for m in messages:
        if m.role == "user":
            current = None  # 新的一轮从下一条 assistant 开始计
            continue
        if m.role != "assistant":
            continue
        uses: list[ToolUseBlock] = m.tool_uses
        if not uses:
            continue
        if current is None:
            current = {"turn": len(turns) + 1, "read_only": [], "write": []}
            turns.append(current)
        for tu in uses:
            safety = (safety_of or (lambda _n: "write"))(tu.name)
            bucket = "read_only" if safety == "readonly" else "write"
            entry = (tu.name, _arg_summary(tu.input, project_root))
            if current[bucket] and _call_key(*current[bucket][-1]) == _call_key(*entry):
                continue
            current[bucket].append(entry)
    return turns


def build_skill_draft(
    messages: list[Message],
    *,
    safety_of: Callable[[str], str] | None = None,
    project_root: Path | str | None = None,
) -> dict:
    """把一次会话的持久化消息整理成 SKILL.md 草稿（不调模型）。

    返回 {"name", "description", "body", "content", "turn_count", "tool_calls"}：
    name / description 是从首条用户消息建议的（前端编辑框可改），body 是
    四小节正文（目标 / 步骤 / 注意事项 / 适用边界），content 是拼好
    frontmatter 的完整 SKILL.md 文本。
    """
    first_user = next(
        (m.text.strip() for m in messages if m.role == "user" and m.text.strip()), ""
    )
    name = suggest_name(first_user)
    description = _generalize_path(
        _first_line(first_user)[:_DESCRIPTION_MAX], project_root
    )
    goal = _generalize_path(first_user, project_root)
    if len(goal) > _GOAL_MAX:
        goal = goal[:_GOAL_MAX] + "…（原文过长已截断）"

    turns = extract_turns(messages, safety_of, project_root)
    write_calls = [(n, s) for t in turns for n, s in t["write"]]
    tool_total = sum(len(t["read_only"]) + len(t["write"]) for t in turns)

    lines: list[str] = []
    lines += ["## 目标", ""]
    lines += [goal or "（会话里没有可引用的用户目标，请补一句这个技能要做什么。）", ""]

    lines += ["## 步骤", ""]
    if not turns:
        lines += [
            "本会话没有实际工具调用（纯对话交流），没有可提取的操作步骤。",
            "请根据目标手动补上具体步骤，或删除本节。",
            "",
        ]
    else:
        for t in turns:
            lines.append(f"### 第 {t['turn']} 轮")
            lines.append("")
            for label, bucket in ((_TURN_READ_ONLY, "read_only"), (_TURN_WRITE, "write")):
                calls = t[bucket]
                if not calls:
                    continue
                hint = "" if bucket == "read_only" else "（默认需要确认）"
                lines.append(f"{label}{hint}：")
                for n, s in calls:
                    lines.append(f"- `{n}`（{s}）" if s else f"- `{n}`")
                lines.append("")
        # 非空轮次段落之间保证有空行收尾（上面循环每桶后已补，这里兜总底）
        if lines[-1] != "":
            lines.append("")

    lines += ["## 注意事项", ""]
    if write_calls:
        lines += [
            "以下写入/执行类操作默认要过权限门：执行时会弹出确认卡片，"
            "需要你点「允许一次」或「总是允许（本项目）」才会真正执行。",
            "换项目复用本技能时，同类操作仍会按目标项目的权限设置再次确认。",
            "",
        ]
        seen: set[tuple[str, str]] = set()
        for n, s in write_calls:
            key = _call_key(n, s)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- `{n}`（{s}）" if s else f"- `{n}`")
    else:
        lines += [
            "本会话只用了只读工具，没有触发权限确认的操作；"
            "补充写入/执行步骤时，记得提醒使用者这些操作需要确认。",
        ]
    lines.append("")

    lines += [
        "## 适用边界",
        "",
        "- 本草稿由一次具体会话自动提炼（未调模型润色）：原会话绑定某个本地项目，"
        "路径已泛化为「<项目>」。",
        "- 步骤中的文件名与相对路径来自原会话的目录结构，换项目使用时需按实际情况调整。",
    ]
    body = "\n".join(lines).strip() + "\n"
    if len(body) > MAX_DRAFT_BODY_CHARS:
        body = (
            body[:MAX_DRAFT_BODY_CHARS]
            + "\n\n（正文超过 20000 字符上限，已截断；请手动精简后再保存。）\n"
        )

    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    return {
        "name": name,
        "description": description,
        "body": body,
        "content": content,
        "turn_count": len(turns),
        "tool_calls": tool_total,
    }


def save_skill_draft(
    root: Path, name: str, content: str, *, existing: set[str]
) -> dict:
    """把编辑过的草稿写成一个技能目录（skills.save_draft 的落盘层）。

    与技能安装同一套落点防线：名字形状过 _validate_skill_name，落点过
    _skill_target（resolve 后必须仍在技能根内），文件经 textio 原子写。
    重名不覆盖（与安装默认一致，报错让用户先删或改名）。

    content 必须是带 frontmatter 的完整 SKILL.md 文本，且 frontmatter 的
    name 与保存名一致——loader 用 frontmatter 的 name 当技能名，目录名却
    来自保存参数，两者不一致会让清单里的名字和编辑框里填的对不上。
    """
    clean = _validate_skill_name(name)
    if clean in existing:
        raise SkillInstallError(
            f"已存在同名技能：{clean}；请先删除或换个名字再保存"
        )
    text = (content or "").strip()
    if not text:
        raise SkillInstallError("草稿内容为空，没有可保存的技能")
    meta, body = _parse_frontmatter(text)
    if not meta:
        raise SkillInstallError(
            "草稿缺少 frontmatter（--- 包围的 name / description 头部）；"
            "请从「存为技能」编辑框重新保存，不要手拼文件"
        )
    fm_name = (meta.get("name") or "").strip()
    if fm_name != clean:
        raise SkillInstallError(
            f"frontmatter 的 name（{fm_name or '空'}）与保存的技能名（{clean}）不一致，"
            "请统一后再保存"
        )
    if not (meta.get("description") or "").strip():
        raise SkillInstallError(
            "frontmatter 缺少 description：技能清单里没有描述会很难辨认"
        )
    if len(body) > MAX_DRAFT_BODY_CHARS:
        raise SkillInstallError(
            f"技能正文超过 {MAX_DRAFT_BODY_CHARS} 字符（load_skill 读取上限），"
            "请精简后再保存"
        )
    target = _skill_target(root, clean)
    md = target / "SKILL.md"
    write_text_atomic(md, text if text.endswith("\n") else text + "\n")
    return {"name": clean, "path": str(md)}
