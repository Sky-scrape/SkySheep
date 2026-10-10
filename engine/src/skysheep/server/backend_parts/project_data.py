"""项目与会话的数据服务：项目任务清单 / 会话导出 / 项目记忆（AGENTS.md）。

从 backend.py 按职责注释整段搬入。方法体逐字保留，逻辑不变。
三块同属「前端侧栏/设置页按项目取存数据」的职责面：项目任务清单（右侧
「任务清单」页签）、会话导出（md / html 单文件）、项目记忆读写（左侧
「项目记忆」入口）；导出辅助（_fmt_export_dur / _export_*_html /
_html_escape / EXPORT_HTML_TEMPLATE）随 export_session 同域落位。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ... import __version__
from ...core.estimate import format_range
from ...core.prompt import MAX_INSTRUCTIONS_CHARS
from ...messages import Message
from ...textio import read_text_file, write_text_file


def _fmt_export_dur(seconds: float) -> str:
    """导出里的时长文案：45 秒 / 3 分 20 秒 / 1 小时 5 分（与界面 fmtEtaDur 同口径）。"""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒" if sec else f"{m} 分钟"
    h, mm = divmod(m, 60)
    return f"{h} 小时 {mm} 分" if mm else f"{h} 小时"


def _export_eta_html(m: Message) -> str:
    """导出 HTML 的用时芯片（无实测耗时的旧消息返回空串，不凭空造记录）。"""
    if not getattr(m, "duration_ms", 0):
        return ""
    est = getattr(m, "estimate", None) or {}
    lo, hi = est.get("min_seconds", 0), est.get("max_seconds", 0)
    text = f"⏱ 用时 {_fmt_export_dur(m.duration_ms / 1000)}"
    if hi:
        text += f" · 预估 {format_range(lo, hi)}"
    title = (
        f' title="预估依据：{_html_escape(str(est.get("basis", "")))}"'
        if est.get("basis") else ""
    )
    return f'<div class="eta"{title}>{_html_escape(text)}</div>'


def _export_thinking_html(m: Message) -> str:
    """导出 HTML 的思考过程折叠块（<details> 无需 JS）；没有推理内容返回空串。"""
    think = m.thinking
    if not think:
        return ""
    ms = m.thinking_ms
    label = "💭 思考过程"
    if ms:
        label += f"（思考 {_fmt_export_dur(ms / 1000)}）"
    return (
        f'<details class="think"><summary>{_html_escape(label)}</summary>'
        f'<div class="think-body">{_html_escape(think)}</div></details>'
    )


def _export_body_text(m: Message) -> str:
    """导出正文：与 to_plain 一致，但省略已单独成段的思考块（避免重复）。

    只对带推理内容的 assistant 消息生效；其余直接走 to_plain。
    """
    if m.role != "assistant" or not m.thinking:
        return m.to_plain()
    kept = m.model_copy(update={
        "content": [b for b in m.content if getattr(b, "type", "") != "thinking"]
    })
    return kept.to_plain()


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 会话导出 HTML 模板：自包含单文件（无外部资源），纸墨配色与桌面端一致
EXPORT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · SkySheep 导出</title>
<style>
  body {{ background: #F5EFDE; color: #1D1A16;
          font: 15px/1.75 "Microsoft YaHei", system-ui, sans-serif; margin: 0; }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 40px 20px 80px; }}
  header {{ border-bottom: 2px solid #1D1A16; padding-bottom: 14px; margin-bottom: 26px; }}
  header h1 {{ font-size: 22px; margin: 0 0 6px; }}
  header .meta {{ color: #6b6459; font-size: 13px; }}
  .msg {{ border: 1.5px solid #c9bfa5; border-radius: 10px; padding: 12px 16px; margin: 12px 0;
          background: #fffdf6; white-space: pre-wrap; overflow-wrap: break-word;
          box-shadow: 0 1px 3px rgba(29,26,22,.08); }}
  .msg.user {{ background: #E8DFC7; border-color: #1D1A16; margin-left: 48px; }}
  .msg.ai {{ margin-right: 48px; }}
  .msg.tool {{ background: #faf7ee; color: #57503f; font-size: 13px; }}
  .msg.tool.err {{ border-color: #b03a2e; }}
  .msg pre {{ margin: 6px 0 0; white-space: pre-wrap; font: 12px/1.6 Consolas, monospace; }}
  .msg .body {{ white-space: pre-wrap; overflow-wrap: break-word; }}
  .eta {{ display: inline-block; margin: 0 0 8px; padding: 2px 9px; font-size: 12px;
          color: #6b6459; border: 1px solid #c9bfa5; border-radius: 999px; }}
  .think {{ margin: 0 0 8px; padding: 6px 10px; background: #faf7ee;
            border: 1px dashed #c9bfa5; border-radius: 8px; }}
  .think summary {{ cursor: pointer; font-size: 12px; color: #6b6459; }}
  .think-body {{ margin-top: 6px; font-size: 13px; color: #57503f;
                 white-space: pre-wrap; overflow-wrap: break-word; }}
</style>
</head>
<body>
<div class="wrap">
<header><h1>{title}</h1><div class="meta">由 SkySheep v{version} 导出于 {date} · 纯本地会话记录</div></header>
{body}
</div>
</body>
</html>
"""


class ProjectDataMixin:
    """项目任务清单 / 会话导出 / 项目记忆。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- 项目任务清单（右侧「任务清单」页签；任务与项目绑定，删项目一并删） ----

    def _target_project_id(self, project_id) -> int:
        pid = int(project_id) if project_id else (self.project.id if self.project else 0)
        if not pid:
            raise RuntimeError("没有当前项目")
        return pid

    async def project_task_list(self, project_id: int | None = None) -> dict:
        pid = self._target_project_id(project_id)
        return {"project_id": pid, "tasks": await self.store.list_project_tasks(pid)}

    async def project_task_add(self, params: dict) -> dict:
        title = str(params.get("title") or "").strip()
        if not title:
            raise RuntimeError("任务内容不能为空")
        pid = self._target_project_id(params.get("project_id"))
        task = await self.store.add_project_task(pid, title[:200], str(params.get("detail") or "")[:2000])
        return {"task": task}

    async def project_task_update(self, params: dict) -> dict:
        tid = int(params.get("id") or 0)
        if not tid:
            raise RuntimeError("缺少任务 id")
        title = params.get("title")
        detail = params.get("detail")
        task = await self.store.update_project_task(
            tid,
            title=(str(title).strip()[:200] or None) if title is not None else None,
            detail=None if detail is None else str(detail)[:2000],
            done=(bool(params["done"]) if "done" in params else None),
        )
        if not task:
            raise RuntimeError("任务不存在")
        return {"task": task}

    async def project_task_delete(self, params: dict) -> dict:
        tid = int(params.get("id") or 0)
        if not tid:
            raise RuntimeError("缺少任务 id")
        removed = await self.store.delete_project_task(tid)
        if not removed:
            raise RuntimeError("任务不存在")
        return {"removed": tid}

    async def export_session(self, session_id: str, *, fmt: str = "md") -> dict:
        """导出会话：fmt=md 为 Markdown 原文；fmt=html 为带样式的自包含单文件。

        归属校验（安全审查 B3）：别的项目的会话不能凭枚举到的 id 导出。
        """
        sess = await self._get_owned_session(session_id)
        msgs = await self.store.load_messages(session_id)
        safe_title = re.sub(r"[^\w\-]+", "_", sess.title or sess.id)[:40] or sess.id
        if fmt == "html":
            body = []
            for m in msgs:
                if m.role == "system":
                    continue
                if m.role == "user":
                    body.append(f'<div class="msg user">{_html_escape(m.text)}</div>')
                elif m.role == "assistant":
                    # 用时芯片：有实测耗时的消息才加（旧消息没有）
                    chip = _export_eta_html(m)
                    # 思考过程：折叠块（<details> 无需 JS，与界面上的可折叠块对应）
                    think = _export_thinking_html(m)
                    body.append(
                        f'<div class="msg ai">{chip}{think}'
                        f'<div class="body">{_html_escape(m.text)}</div></div>'
                    )
                elif m.role == "tool":
                    for b in m.content:
                        content = getattr(b, "content", "")
                        err = " err" if getattr(b, "is_error", False) else ""
                        body.append(
                            f'<div class="msg tool{err}">🔧 工具结果 · '
                            f'<pre>{_html_escape(content[:1500])}</pre></div>'
                        )
            html = EXPORT_HTML_TEMPLATE.format(
                title=_html_escape(sess.title or sess.id),
                date=date.today().isoformat(),
                version=__version__,
                body="\n".join(body),
            )
            return {"filename": f"skysheep-{safe_title}.html", "html": html}
        lines = [f"# SkySheep 会话 · {sess.title or sess.id}", ""]
        for m in msgs:
            if m.role == "system":
                continue
            lines.append(f"## {m.role}")
            # 用时：随正文标题行一起给出（旧消息没有实测耗时则省略）
            if m.role == "assistant" and getattr(m, "duration_ms", 0):
                est = getattr(m, "estimate", None) or {}
                lo, hi = est.get("min_seconds", 0), est.get("max_seconds", 0)
                eta = f"用时 {_fmt_export_dur(m.duration_ms / 1000)}"
                if hi:
                    eta += f" · 预估 {format_range(lo, hi)}"
                lines.append("")
                lines.append(f"> ⏱ {eta}")
            # 思考过程：导出时保留完整推理文本（界面里是折叠块，导出用引用块区分）。
            # 单独成段后，正文不再重复带 [thinking]（见 _export_body_text）。
            think = m.thinking if m.role == "assistant" else ""
            if think:
                lines.append("")
                lines.append("### 💭 思考过程")
                lines.append("")
                lines.extend(f"> {ln}" for ln in think.splitlines())
            lines.append("")
            lines.append(_export_body_text(m))
            lines.append("")
        return {
            "filename": f"skysheep-{safe_title}.md",
            "markdown": "\n".join(lines),
        }

    # ---- 项目记忆（AGENTS.md 等，前端侧栏「项目记忆」入口） ----

    async def get_instructions(self) -> dict:
        text = ""
        enc_text = ""
        certain = True
        if self.instructions_file:
            p = Path(self.instructions_file)
            if p.is_file():
                try:
                    # 走 textio 探测编码：GBK 等非 UTF-8 的 AGENTS.md 不再被读成
                    # 一串替换字符（此前 utf-8+replace 读完一旦保存就把乱码写死了）
                    loaded = read_text_file(p)
                    text = loaded.text
                    if loaded.encoding != "utf-8":
                        enc_text = loaded.encoding.upper()
                    certain = loaded.certain
                except OSError:
                    text = ""
        mtime = 0.0
        if self.instructions_file:
            try:
                mtime = round(Path(self.instructions_file).stat().st_mtime, 3)
            except OSError:
                mtime = 0.0
        # mtime 供右侧面板保存时比对：编辑期间定期整理重写过 AGENTS.md 就拒绝覆盖
        return {"path": self.instructions_file, "text": text, "mtime": mtime,
                "encoding_text": enc_text, "editable": certain}

    async def save_instructions(self, text: str, base_mtime: float | None = None) -> dict:
        """保存项目记忆并立即刷新系统提示词（对当前会话也生效）。

        落盘走 textio：沿用文件原本的编码与行尾符（新建文件 UTF-8 + LF），
        原子写；超出 MAX_INSTRUCTIONS_CHARS 的部分截掉，但不静默——truncated /
        original_chars / limit 随结果带回，由界面明示。"""
        if base_mtime is not None and float(base_mtime) > 0 and self.instructions_file:
            try:
                cur = round(Path(self.instructions_file).stat().st_mtime, 3)
            except OSError:
                cur = 0.0
            if cur != float(base_mtime):
                raise RuntimeError(
                    "项目记忆在你编辑期间被更新过（如定期整理已重写 AGENTS.md），"
                    "本次保存已阻止；请点「重读」后再编辑保存"
                )
        original_chars = len(text)
        truncated = original_chars > MAX_INSTRUCTIONS_CHARS
        text = text[:MAX_INSTRUCTIONS_CHARS]
        if self.instructions_file:
            path = Path(self.instructions_file)
        else:
            path = Path(self.working_dir) / "AGENTS.md"
            self.instructions_file = str(path)
        # 沿用原编码与行尾符；文件还不存在（首次创建）时按 UTF-8 + LF。
        # 编码探不确定意味着原文件读出来就带替换字符——按它写回等于把乱码固化，
        # 按项目规范「宁可拒绝写」，直接报错指路
        enc, nl = "utf-8", "\n"
        if path.is_file():
            try:
                loaded = read_text_file(path)
            except OSError:
                loaded = None
            if loaded is not None and not loaded.certain:
                raise RuntimeError(
                    "这份 AGENTS.md 的编码无法识别（读出来是替换字符），"
                    "为避免写坏原文件已拒绝保存；请在外部编辑器里把它转成 UTF-8 后重试"
                )
            if loaded is not None:
                enc, nl = loaded.encoding, loaded.newline
        try:
            write_text_file(path, text, enc, nl)
        except (OSError, UnicodeEncodeError) as e:
            raise RuntimeError(f"写入失败: {e}") from None
        self.instructions_text = text
        self._refresh_system_prompts()
        try:
            mtime = round(path.stat().st_mtime, 3)
        except OSError:
            mtime = 0.0
        return {"saved": True, "path": str(path), "chars": len(text), "mtime": mtime,
                "truncated": truncated, "original_chars": original_chars,
                "limit": MAX_INSTRUCTIONS_CHARS}
