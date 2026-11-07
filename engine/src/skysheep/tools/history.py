"""会话历史召回工具：按关键词在当前会话的完整历史里找原文。

为什么需要它：`core/context.py: compact_history` 压缩历史时会把中间段换成
一份摘要，原始消息**在模型眼里彻底消失**（摘要必然丢路径、数字、报错原文）。
但它们在 `messages` 表里一直躺着——差的只是一个入口。这个工具就是那个入口，
与压缩时落盘的 `.skysheep/transcripts/` 一起构成「摘要 + 可回溯」的两段式：
摘要在窗内保上下文连续，原文按需召回（借鉴 Super-agent 的 Inline / Spill）。

只读、只查本会话：跨会话搜索是给用户找「这事在哪个会话聊的」，语义不同，
且把别的会话内容摊给模型是越界。项目级/全局搜索走界面里的搜索框。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError


class SearchHistoryArgs(BaseModel):
    query: str = Field(
        description="要召回的关键词或短语（文件名、函数名、错误码、路径片段都行）"
    )
    limit: int = Field(
        default=8, ge=1, le=20, description="最多返回几条命中（按时间倒序）"
    )


class SearchHistoryTool(Tool):
    """在本会话的历史消息里按关键词召回原文片段。"""

    name = "search_history"
    description = (
        "Search this conversation's own earlier messages for a keyword and get the "
        "original text back. Use it when the earlier-conversation-summary lost a "
        "detail you need (a path, a number, an exact error message, a decision) — "
        "the full messages are still stored even after the history was compacted."
    )
    safety = Safety.READONLY
    # 纯读召回：只查本会话历史库，不改任何状态、不触外部实体，
    # 同参重查结果一致——与 read_file 同档取值（基类缺省是「可能有破坏性」，
    # 纯读工具不显式声明会被外部宿主多警告一次）
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = SearchHistoryArgs

    def __init__(self, store=None) -> None:
        # store 由装配层注入（tools/__init__.py: default_tools）；None 时工具仍
        # 注册但调用会给可读提示，而不是从注册表里消失（模型看得见能力边界）。
        self.store = store

    async def run(self, args: SearchHistoryArgs, ctx: ToolContext) -> str:
        if self.store is None:
            raise ToolError("历史检索不可用（未接入会话存储）")
        sid = str(getattr(ctx, "session_id", "") or "")
        if not sid:
            raise ToolError("当前没有绑定的会话，无法检索历史")
        query = (args.query or "").strip()
        if not query:
            raise ToolError("query 不能为空")
        rows = await self.store.search_session_history(sid, query, limit=args.limit)
        if not rows:
            return (
                f"本会话历史里没有找到「{query}」。\n"
                "可以换一个更短的关键词（文件名、错误码、函数名）再试；"
                "也可以直接看 .skysheep/transcripts/ 下压缩时落盘的完整原文。"
            )
        lines = [f"本会话历史中命中「{query}」{len(rows)} 条（按时间倒序）：", ""]
        for r in rows:
            body = r["text"].strip().replace("\n", " ")
            if r.get("truncated"):
                body += "…（已截断）"
            lines.append(f"#{r['seq']} [{r['role']}] {body}")
        lines += [
            "",
            "以上是原文片段（压缩前的完整内容仍存在会话库里）。"
            "需要更完整的上下文时，请读压缩摘要里给出的 transcripts 文件路径。",
        ]
        return "\n".join(lines)
