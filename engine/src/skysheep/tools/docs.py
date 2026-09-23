"""read_document / write_document：文档的读取与生成。

read_document：把 PDF / Word / Excel 文档提取为纯文本。普通用户最自然的用法是
「把这个 PDF/Word 发给 AI 看看」，而 read_file 对二进制文件无能为力。本工具补上
这一环：只提取文本（含表格），不做渲染。

- PDF（pypdf）：逐页提取，带「--- 第 N 页 ---」分页标记；
- Word .docx（python-docx）：段落 + 表格（按行用 | 拼接）；
- Excel .xlsx（openpyxl）：逐工作表逐行，单元格用 | 拼接。

write_document：反向生成 .docx / .xlsx / .csv——用户「帮我整理成 Word 报告 /
Excel 表格」的高频诉求。模型给的 content 是人类可读的源格式（Markdown 子集 /
JSON 行数据 / CSV 文本），由本工具转换为真正的 Office 文件；生成走 WRITE 权限
确认并接入检查点记录器，与 write_file 同一套安全模型。

解析与生成是 CPU 活且可能较慢，放到线程里跑（asyncio.to_thread），不卡事件循环。
只读工具（Safety.READONLY）自动放行；写文档与 write_file 一样默认需确认。
依赖缺失时给出可操作的中文提示。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .base import (
    ChangeRecorder,
    Safety,
    Tool,
    ToolContext,
    ToolError,
    check_write_size,
    rel_path,
    resolve_path,
    truncate_output,
)

DEFAULT_MAX_CHARS = 20_000
MAX_DOC_BYTES = 50_000_000  # 50MB：文档类文件的超上限基本是拿错了文件

SUPPORTED = (".pdf", ".docx", ".xlsx", ".pptx")


def extract_document_text(p: Path, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """按扩展名提取文档文本（PDF/DOCX/XLSX），工具与文件树预览共用。

    同步函数（CPU 解析），调用方自行放线程池；失败抛 ToolError（中文可读）。
    """
    suffix = p.suffix.lower()
    if suffix not in SUPPORTED:
        raise ToolError(f"只支持 {' / '.join(SUPPORTED)} 文档：{suffix or '(无扩展名)'}")
    try:
        if suffix == ".pdf":
            return _extract_pdf(p, max_chars)
        if suffix == ".docx":
            return _extract_docx(p)
        if suffix == ".pptx":
            return _extract_pptx(p)
        return _extract_xlsx(p, max_chars)
    except ToolError:
        raise
    except ImportError as e:
        raise ToolError(
            f"文档解析库未安装（{e.name or e}）。请在引擎目录执行 "
            "`uv sync` 重装依赖后重试。"
        ) from e
    except Exception as e:  # noqa: BLE001 - 解析器内部错误统一友好提示
        raise ToolError(f"文档解析失败：{type(e).__name__}: {e}") from e


class ReadDocumentArgs(BaseModel):
    path: str = Field(description="文档路径（相对当前工作目录或绝对路径），支持 .pdf / .docx / .xlsx")
    max_chars: int = Field(
        default=DEFAULT_MAX_CHARS, ge=500, le=200_000, description="返回文本的最大字符数"
    )


def _extract_pdf(p: Path, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(p))
    if reader.is_encrypted:
        raise ToolError("PDF 已加密，请先解密后再发给我")
    parts: list[str] = []
    total = 0
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001 - 单页损坏不拖垮整个文档
            text = f"(第 {i} 页解析失败: {e})"
        parts.append(f"--- 第 {i} 页 ---\n{text}")
        total += len(text) + 20
        if total > max_chars * 2:  # 提取层面提前收手，避免超长 PDF 白耗时
            parts.append(f"(已达 {max_chars * 2} 字符上限，后面 {len(reader.pages) - i} 页省略)")
            break
    text = "\n\n".join(parts)
    return f"[PDF 共 {len(reader.pages)} 页]\n\n" + text


def _extract_docx(p: Path) -> str:
    import docx  # python-docx

    d = docx.Document(str(p))
    parts: list[str] = []
    # 正文段落与表格按文档顺序交错输出：iter_inner_content 保留原始顺序
    try:
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        for block in d.iter_inner_content():
            if isinstance(block, Paragraph):
                if block.text.strip():
                    parts.append(block.text)
            elif isinstance(block, Table):
                for row in block.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    parts.append("| " + " | ".join(cells) + " |")
    except AttributeError:  # 老版本 python-docx 没有 iter_inner_content
        for para in d.paragraphs:
            if para.text.strip():
                parts.append(para.text)
        for table in d.tables:
            for row in table.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                parts.append("| " + " | ".join(cells) + " |")
    text = "\n".join(parts)
    return f"[Word 文档]\n\n{text}"


def _extract_pptx(p: Path) -> str:
    """抽取 PPT 文本：每页标出页码、标题、正文层级与备注。

    包含备注：演示稿的要点往往写在备注里，只读正文会漏掉一半信息。
    图层形状的位置不保证与阅读顺序一致（pptx 是绝对定位的），所以只能按
    「形状在 XML 里的顺序」输出；表格按行展开为 | 分隔。
    """
    from pptx import Presentation

    prs = Presentation(str(p))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        lines.append("| " + " | ".join(cells) + " |")
                continue
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if not text:
                    continue
                indent = "  " * min(para.level or 0, 4)
                lines.append(indent + text)
        notes = ""
        if slide.has_notes_slide:
            note_text = (slide.notes_slide.notes_text_frame.text or "").strip()
            if note_text:
                notes = "\n[备注] " + note_text.replace("\n", " / ")
        body = "\n".join(lines) if lines else "(本页无文本，可能只有图片)"
        parts.append(f"--- 第 {i} 页 ---\n{body}{notes}")
    return f"[PPT 演示文稿，共 {len(prs.slides)} 页]\n\n" + "\n\n".join(parts)


def _extract_xlsx(p: Path, max_chars: int) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            parts.append(f"=== 工作表: {ws.title} ===")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    parts.append(" | ".join(cells).rstrip(" |"))
                if sum(len(x) for x in parts[-10:]) > max_chars * 2:
                    parts.append("(行数过多，已截断)")
                    break
    finally:
        wb.close()
    return "[Excel 工作簿]\n\n" + "\n".join(parts)


class ReadDocumentTool(Tool):
    name = "read_document"
    description = (
        "读取 PDF / Word(.docx) / Excel(.xlsx) / PPT(.pptx) 文档的文本内容（PDF 按页、"
        "Word 含表格、Excel 按工作表逐行、PPT 按页含备注）。"
        "纯文本文件请用 read_file。用户让你「看/读/总结某个文档」时用它。"
    )
    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = ReadDocumentArgs

    async def run(self, args: ReadDocumentArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        if not p.exists() or p.is_dir():
            raise ToolError("file not found: " + shown)
        suffix = p.suffix.lower()
        if suffix not in SUPPORTED:
            raise ToolError(
                f"read_document 只支持 {' / '.join(SUPPORTED)} 文件，"
                f"{suffix or '(无扩展名)'} 请用 read_file"
            )
        try:
            if p.stat().st_size > MAX_DOC_BYTES:
                raise ToolError(f"文件太大（{p.stat().st_size / 1048576:.0f}MB），上限 50MB")
        except OSError as e:
            raise ToolError("cannot read " + shown + ": " + str(e)) from e

        text = await asyncio.to_thread(extract_document_text, p, args.max_chars)

        if not text.strip():
            raise ToolError("文档里没有可提取的文本（可能是纯图片扫描件，本工具不识别图片内容）")
        header, _, body = text.partition("\n\n")
        return f"{header}（{shown}）\n\n" + truncate_output(body, args.max_chars)


# ===================== write_document：生成 Word / Excel / CSV =====================

WRITE_SUPPORTED = (".docx", ".xlsx", ".csv", ".pptx")


class WriteDocumentArgs(BaseModel):
    path: str = Field(
        description="目标文档路径（相对当前工作目录或绝对路径），扩展名决定格式："
        ".docx（Word）/ .xlsx（Excel）/ .csv / .pptx（演示文稿）"
    )
    content: str = Field(
        description="文档内容源格式——.docx：Markdown 子集（#/##/### 标题、段落、"
        "- 列表、| 表格 |、**加粗**、`代码`）；.xlsx：JSON 对象 {\"工作表名\": "
        "[[单元格,...],...]}，单元格为字符串/数字/布尔/null；.csv：CSV 文本；"
        ".pptx：Markdown 子集，## 一级标题开一页（首行 # 作为封面标题），"
        "- 列表变成项目符号，> 引用作为该页演讲者备注"
    )


def _docx_add_runs(paragraph, text: str) -> None:
    """把一行 Markdown 行内语法（**加粗** / `代码` / [链接](url)）写进段落。"""
    text = re.sub(r"\[([^\]]+)\]\((https?:[^)]+)\)", r"\1（\2）", text)
    for part in re.split(r"(\*\*.+?\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            r = paragraph.add_run(part[2:-2])
            r.bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            r = paragraph.add_run(part[1:-1])
            r.font.name = "Consolas"
        else:
            paragraph.add_run(part)


def _docx_add_lines(paragraph, text: str) -> None:
    """段落内多行用软换行（run.add_break）保留原始行结构。"""
    for i, line in enumerate(text.split("\n")):
        if i:
            paragraph.add_run().add_break()
        if line:
            _docx_add_runs(paragraph, line)


def build_docx(target: Path, content: str) -> str:
    """Markdown 子集 → .docx（同步，线程里跑）。返回结果摘要。"""
    import docx
    from docx.oxml.ns import qn
    from docx.shared import Pt

    d = docx.Document()
    # 正文默认字体：西文 Calibri + 中文微软雅黑（默认模板对中文显示不友好）
    normal = d.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    lines = content.replace("\r\n", "\n").split("\n")
    para: list[str] = []
    tables = {"added": 0}
    headings = {"n": 0}

    def flush_para() -> None:
        if not para:
            return
        p = d.add_paragraph()
        _docx_add_lines(p, "\n".join(para))
        para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            flush_para()
            d.add_heading(m.group(2).strip(), level=len(m.group(1)))
            headings["n"] += 1
            i += 1
            continue
        if stripped.startswith("|"):
            flush_para()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cells):  # 跳过分隔行
                    rows.append(cells)
                i += 1
            if rows:
                cols = max(len(r) for r in rows)
                table = d.add_table(rows=len(rows), cols=cols)
                table.style = "Table Grid"
                for ri, row in enumerate(rows):
                    for ci in range(cols):
                        cell = table.cell(ri, ci)
                        cell.text = ""
                        _docx_add_runs(cell.paragraphs[0], row[ci] if ci < len(row) else "")
                        if ri == 0:  # 首行加粗当表头
                            for r in cell.paragraphs[0].runs:
                                r.bold = True
                tables["added"] += 1
            continue
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            flush_para()
            p = d.add_paragraph(style="List Bullet")
            _docx_add_runs(p, m.group(1))
            i += 1
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            flush_para()
            p = d.add_paragraph(style="List Number")
            _docx_add_runs(p, m.group(1))
            i += 1
            continue
        if stripped.startswith(">"):
            flush_para()
            p = d.add_paragraph(style="Intense Quote")
            _docx_add_runs(p, stripped.lstrip("> "))
            i += 1
            continue
        if stripped in ("---", "***", "___"):
            flush_para()  # 水平分割线：跳过（Word 里很少需要）
            i += 1
            continue
        if not stripped:
            flush_para()
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        d.save(str(target))
    except OSError as e:
        raise ToolError(f"cannot write {target.name}: {e}") from e
    n_para = sum(1 for p in d.paragraphs if p.text.strip())
    return f"Word 文档已生成：{n_para} 个段落、{headings['n']} 个标题、{tables['added']} 张表格"


def _xlsx_cell(v):  # noqa: ANN001 - 任意 JSON 值
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return json.dumps(v, ensure_ascii=False)


def build_xlsx(target: Path, content: str) -> str:
    """JSON 行数据 → .xlsx（同步，线程里跑）。返回结果摘要。

    接受三种形态：
    {"工作表": [[r1c1, r1c2], [r2c1, ...]], ...}
    [{"name": "工作表", "rows": [[...]]}, ...]
    [[r1c1, ...], ...]（单表，名为 Sheet1）
    """
    import openpyxl

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ToolError(
            ".xlsx 的 content 必须是 JSON。示例：{\"销售数据\": [[\"月份\", \"销售额\"], "
            "[\"1月\", 12000]]}。工作表名到二维数组；单元格可为 字符串/数字/布尔/null。"
        ) from e

    sheets: list[tuple[str, list[list]]] = []
    if isinstance(data, dict):
        sheets = [(str(k), v) for k, v in data.items()]
    elif isinstance(data, list) and data and all(isinstance(s, dict) and "rows" in s for s in data):
        sheets = [(str(s.get("name") or f"Sheet{i + 1}"), s["rows"]) for i, s in enumerate(data)]
    elif isinstance(data, list):
        sheets = [("Sheet1", data)]
    else:
        raise ToolError(".xlsx 的 content JSON 结构不认识：请用 {\"工作表名\": [[...]]} 或 "
                        "[{\"name\": ..., \"rows\": [[...]]}] 或 [[...]]（单表）")
    if not sheets:
        raise ToolError(".xlsx 的 content 里没有任何工作表")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    total_rows = 0
    for name, rows in sheets:
        if not isinstance(rows, list):
            raise ToolError(f"工作表「{name}」的行数据必须是二维数组")
        ws = wb.create_sheet(title=name[:31])  # Excel 工作表名上限 31 字符
        widths: list[int] = []
        for row in rows:
            if not isinstance(row, list):
                raise ToolError(f"工作表「{name}」里出现了非数组的行：{str(row)[:60]}")
            ws.append([_xlsx_cell(v) for v in row])
            total_rows += 1
            for ci, v in enumerate(row):
                w = len(str(v)) if v is not None else 0
                if ci < len(widths):
                    widths[ci] = max(widths[ci], w)
                else:
                    widths.append(w)
        for ci, w in enumerate(widths[:50], start=1):  # 近似列宽，超宽截断
            ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = min(w + 4, 60)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(target))
    except OSError as e:
        raise ToolError(f"cannot write {target.name}: {e}") from e
    names = "/".join(n for n, _ in sheets)
    return f"Excel 已生成：{len(sheets)} 个工作表（{names}）、共 {total_rows} 行"


class _Slide:
    """构建中的一页：标题 + 正文行 + 备注。"""

    def __init__(self, title: str = "") -> None:
        self.title = title
        self.bullets: list[tuple[int, str]] = []  # (层级, 文本)
        self.notes: list[str] = []
        self.paragraphs: list[str] = []


def _parse_slide_markdown(content: str) -> tuple[str, list[_Slide]]:
    """Markdown 子集 → (封面标题, 页列表）。

    约定（写在工具描述里，模型需按此产出）：
    - 首行 `# 标题` 作封面；后面每个 `##` 开一页；
    - `###` 作为页内小标题（当项目符号写）；`-` / `*` / 数字列表作为项目符号；
    - `> 内容` 归入当前页的演讲者备注；
    - 其余行按段落写入正文。
    """
    cover = ""
    slides: list[_Slide] = []
    cur: _Slide | None = None

    for raw in content.replace("\r\n", "\n").split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if level == 1 and not cover and cur is None:
                cover = title
                continue
            if level <= 2:
                cur = _Slide(title)
                slides.append(cur)
                continue
            # ### 及更深：页内小标题，作为顶层项目符号
            if cur is None:
                cur = _Slide()
                slides.append(cur)
            cur.bullets.append((0, title))
            continue
        if stripped.startswith(">"):
            text = stripped.lstrip("> ").strip()
            target = cur or (slides[-1] if slides else None)
            if target is None:
                cover = cover or text
                continue
            target.notes.append(text)
            continue
        if cur is None:
            # 没有 ## 之前的散行：归入封面副标题（作为第一页的引言）
            cur = _Slide()
            slides.append(cur)
        bullet = re.match(r"^([-*+])\s+(.*)$", stripped)
        number = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if bullet or number:
            text = (bullet or number).group(2).strip()
            # 两空格缩进 = 降一级，支持简单层级
            indent = len(raw) - len(raw.lstrip(" \t"))
            cur.bullets.append((min(indent // 2, 2), text))
            continue
        cur.paragraphs.append(stripped)
    return cover, slides


def build_pptx(target: Path, content: str) -> str:
    """Markdown 子集 → .pptx（同步，线程里跑）。返回结果摘要。

    用 python-pptx 的默认模板：版式取自 slide_layouts（0=标题页、1=标题+内容），
    不引入外部主题文件，保证装了依赖就能跑。正文字号随内容量自适应，
    避免文字溢出幻灯片（超量时给出提示而不是默默截断）。
    """
    from pptx import Presentation
    from pptx.util import Pt

    cover, slides = _parse_slide_markdown(content)
    if not cover and not slides:
        raise ToolError("content 里没有可生成的内容（至少给一个 `# 标题` 或 `## 页标题`）")

    prs = Presentation()
    title_layout, body_layout = prs.slide_layouts[0], prs.slide_layouts[1]

    if cover:
        slide = prs.slides.add_slide(title_layout)
        slide.shapes.title.text = cover
        subtitle = slide.placeholders[1]
        first = slides[0] if slides else None
        subtitle.text = " ".join(first.paragraphs)[:200] if first and first.paragraphs else ""

    overflow: list[int] = []
    for idx, s in enumerate(slides, start=1):
        slide = prs.slides.add_slide(body_layout)
        slide.shapes.title.text = s.title or f"第 {idx} 页"
        body = slide.placeholders[1]
        tf = body.text_frame
        tf.word_wrap = True
        items: list[tuple[int, str]] = list(s.bullets)
        items += [(0, p) for p in s.paragraphs]
        if not items:
            items = [(0, "")]
        # 内容多则缩小字号：把溢出挡在生成阶段
        size = 20 if len(items) <= 6 else (17 if len(items) <= 10 else 14)
        if len(items) > 16:
            overflow.append(idx)
        first_item = True
        for level, text in items:
            para = tf.paragraphs[0] if first_item else tf.add_paragraph()
            first_item = False
            para.text = text
            para.level = min(level, 4)
            for run in para.runs:
                run.font.size = Pt(size - min(level, 2) * 2)
        if s.notes:
            slide.notes_slide.notes_text_frame.text = "\n".join(s.notes)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        prs.save(str(target))
    except OSError as e:
        raise ToolError(f"cannot write {target.name}: {e}") from e
    summary = f"PPT 已生成：{len(prs.slides)} 页"
    if overflow:
        summary += (
            f"；第 {'、'.join(str(i) for i in overflow)} 页内容较多（超过 16 条），"
            "已自动缩小字号，建议拆分成多页"
        )
    return summary


class WriteDocumentTool(Tool):
    name = "write_document"
    description = (
        "生成 Office 文档文件：.docx（Word，content 用 Markdown 子集：#/##/### 标题、"
        "段落、- 列表、| 表格 |、**加粗**、`代码`）；.xlsx（Excel，content 用 JSON："
        "{\"工作表名\": [[单元格,...],...]}，单元格为 字符串/数字/布尔/null）；.csv（content "
        "直接给 CSV 文本，带 BOM 可被 Excel 正确打开）；.pptx（PPT，content 用 Markdown "
        "子集：# 标题作封面、## 开新页、- 列表变项目符号、> 引用作演讲者备注）。"
        "普通文本文件请用 write_file。用户要 Word/Excel/PPT 报告、表格、清单、"
        "演示稿时用它。"
    )
    safety = Safety.WRITE
    write_path_arg = True
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = WriteDocumentArgs

    def __init__(self, recorder: ChangeRecorder | None = None) -> None:
        self.recorder = recorder

    async def run(self, args: WriteDocumentArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        suffix = p.suffix.lower()
        if suffix not in WRITE_SUPPORTED:
            raise ToolError(
                f"write_document 只支持 {' / '.join(WRITE_SUPPORTED)}，"
                f"{suffix or '(无扩展名)'} 是文本文件请用 write_file"
            )
        if p.exists() and p.is_dir():
            raise ToolError(shown + " 是目录，不能写入")
        content = args.content.strip()
        if not content:
            raise ToolError("content 不能为空")
        check_write_size(content, shown)
        existed = p.exists()
        if self.recorder is not None:
            self.recorder.record(p)  # 检查点：改前不存在 → 回滚时删除
        try:
            if suffix == ".docx":
                summary = await asyncio.to_thread(build_docx, p, content)
            elif suffix == ".xlsx":
                summary = await asyncio.to_thread(build_xlsx, p, content)
            elif suffix == ".pptx":
                summary = await asyncio.to_thread(build_pptx, p, content)
            else:  # .csv：utf-8-sig（带 BOM），Excel 双击打开中文不乱码
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content.replace("\r\n", "\n") + "\n", encoding="utf-8-sig")
                summary = f"CSV 已生成：{len(content.splitlines())} 行"
        except ToolError:
            raise
        except ImportError as e:
            raise ToolError(
                f"文档生成库未安装（{e.name or e}）。请在引擎目录执行 `uv sync` 重装依赖后重试。"
            ) from e

        # 确认弹窗/工具卡的改动预览：新建文档展示源内容 diff；覆盖已有文档（旧内容是
        # 二进制）给不出有意义的文本 diff，保持为空
        ctx.last_diff = ""
        if not existed and suffix != ".csv":
            from .fs import make_diff

            ctx.last_diff = make_diff("", content, shown)
        return f"{summary} → {shown}"
