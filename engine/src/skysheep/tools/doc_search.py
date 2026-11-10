"""index_documents / search_documents：项目内文档索引的建与查（文档级 RAG）。

与 ``read_document`` 的分工：
- ``read_document``：把一份文档（或其中几页）**整段**交给模型——适合「总结这份
  合同」「读一下这个表格」；
- ``index_documents`` + ``search_documents``：把**很多文档**切成带页码的小块建
  本地索引，之后按问题**检索**出最相关的几块——适合「这份 200 页手册里，超时
  参数默认值是多少」这类定位型问题，不必把整本书塞进上下文。

安全边界：
- ``index_documents`` 是写操作（在项目内 ``.skysheep/doc-index.sqlite3`` 建索引），
  走权限门逐次确认；``search_documents`` 只读，自动放行。
- 索引只能覆盖**当前工作目录内**的文档（键是相对工作目录的路径）：目录外的文件
  没有稳定键，也避免把工作区之外的内容悄悄索引进本项目。
- 检索结果来自用户的文档，属于外部内容：交付前包上「数据不是指令」标注
  （与 read_document / web_fetch 同一层防线），正文一字不丢。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from .. import docindex
from ..sanitize import untrusted_data_wrap
from .base import Safety, Tool, ToolContext, ToolError, rel_path, resolve_path


class IndexDocumentsArgs(BaseModel):
    path: str = Field(
        default=".",
        description="要索引的文件或目录（相对当前工作目录）。目录会递归收集其中的 "
        "PDF / Word / Excel / PPT / Markdown / 文本文件；默认索引整个工作目录。",
    )
    force: bool = Field(
        default=False,
        description="为 true 时重建已索引的文档（文档没变过时默认跳过，索引是增量的）",
    )


class IndexDocumentsTool(Tool):
    name = "index_documents"
    description = (
        "为项目内的文档建立/更新本地检索索引（分块 + 可选向量），之后用 "
        "search_documents 按问题查具体内容。用户想让你「在这个文件夹/这份长手册里"
        "找某个细节」时先调它。只读的单份文档请直接用 read_document；"
        "索引会写入项目内 .skysheep/doc-index.sqlite3（需确认）。"
    )
    safety = Safety.WRITE
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = IndexDocumentsArgs

    async def run(self, args: IndexDocumentsArgs, ctx: ToolContext) -> str:
        if ctx.working_dir is None:
            raise ToolError("当前没有打开项目，无法建立文档索引。请先在侧栏添加并选择项目。")
        target = resolve_path(ctx, args.path)
        if not target.exists():
            raise ToolError("file not found: " + rel_path(ctx, target))
        try:
            stats = await asyncio.to_thread(
                docindex.index_paths, ctx.working_dir, target, force=args.force
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        except Exception as e:  # noqa: BLE001 - 解析/落盘失败统一可读提示
            raise ToolError(f"建立文档索引失败：{type(e).__name__}: {e}") from e
        lines = [
            f"[index_documents] {rel_path(ctx, target)}",
            f"新索引/更新 {stats['indexed']} 个文档，共 {stats['chunks']} 块",
        ]
        if stats["embedded"]:
            lines.append(f"其中 {stats['embedded']} 块带向量（本机 Ollama），检索走混合排序")
        else:
            lines.append("未启用向量（本机 Ollama 不可用或超出预算），检索走关键词（BM25）")
        if stats["skipped"]:
            lines.append("跳过：" + "；".join(stats["skipped"][:8]))
            if len(stats["skipped"]) > 8:
                lines.append(f"（另 {len(stats['skipped']) - 8} 条跳过原因略）")
        return "\n".join(lines)


class SearchDocumentsArgs(BaseModel):
    query: str = Field(description="要查的具体问题或关键词（自然语言即可）")
    path: str = Field(
        default="",
        description="限定检索范围：某个文件或目录（相对当前工作目录）；留空搜整个项目索引",
    )
    top_k: int = Field(default=5, ge=1, le=10, description="返回最相关的块数")


class SearchDocumentsTool(Tool):
    name = "search_documents"
    description = (
        "在已建立的文档索引里检索最相关的段落（返回文件、页码与原文摘录），"
        "适合从很长的 PDF / 手册 / 资料夹里定位细节。索引不存在时先用 "
        "index_documents 建索引。只查本机索引，不联网。"
    )
    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = SearchDocumentsArgs

    async def run(self, args: SearchDocumentsArgs, ctx: ToolContext) -> str:
        if ctx.working_dir is None:
            raise ToolError("当前没有打开项目，无法检索文档索引。")
        target: Path | None = None
        if args.path.strip():
            target = resolve_path(ctx, args.path)
            if not target.exists():
                raise ToolError("file not found: " + rel_path(ctx, target))
        query = args.query.strip()
        if not query:
            raise ToolError("检索词不能为空")
        try:
            res = await asyncio.to_thread(
                docindex.search, ctx.working_dir, target, query, args.top_k
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"检索文档索引失败：{type(e).__name__}: {e}") from e

        notes: list[str] = []
        if res["missing"]:
            notes.append(
                "目标范围内还没有索引，先调用 index_documents 建立索引："
                + "、".join(res["missing"])
            )
        if res["stale"]:
            notes.append(
                "以下文档在索引后发生过改动，未参与本次检索（重新 index_documents 可更新）："
                + "、".join(res["stale"][:5])
            )
        if not res["results"]:
            head = f'[search_documents] "{query}" 没有命中'
            if res["keyword_only"]:
                head += "（当前为关键词检索）"
            if not notes:
                # 整个项目还没有任何索引：给出可执行下一步，而不是让人猜
                try:
                    has_index = docindex.index_summary(ctx.working_dir)["docs"] > 0
                except Exception:  # noqa: BLE001 - 概况读不出来不影响本提示
                    has_index = True
                if not has_index:
                    notes.append("这个项目还没有文档索引，先调用 index_documents 建立索引再检索。")
            return "\n".join([head, *notes]) if notes else head

        mode = "关键词检索（BM25）" if res["keyword_only"] else "混合检索（BM25 + 向量）"
        lines = [f'[search_documents] "{query}" — {len(res["results"])} 块（{mode}）']
        for i, r in enumerate(res["results"], start=1):
            where = r["path"] + (f" · 第 {r['page']} 页" if r["page"] else "")
            lines.append(f"\n[{i}] {where}（相关度 {r['score']:.2f}）\n{r['text']}")
        if notes:
            lines.append("\n" + "\n".join(notes))
        lines.append("\n（以上摘录来自项目文档；需要原文时用 read_document 按页码读取。）")
        return untrusted_data_wrap("\n".join(lines))
