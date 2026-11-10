"""来源登记簿：本轮工具读到/搜到的网页 → 稳定编号 → 随回答落库与前端展示。

动机：web_fetch / web_search 的内容进了上下文，但回答里「据官方文档」这类说法
没有可核链接——用户没法点回去验证，也对不上模型引用的第几条。这里给每次抓取
分配一个轮内编号（``[来源 1]``），同一 URL 复用同一编号（模型反复抓同一页不会
出现两个来源），轮末把整张表挂到最终 assistant 消息上：

- 工具输出里嵌入 ``[来源 N]`` 标签，模型要引用时直接抄编号；
- 消息落库带上 ``sources`` 字段（历史恢复/前端 chip 都靠它，不依赖模型自觉
  在正文里写编号——它是「这一轮查过什么」的客观记录）。

编号是**轮内**的：下一轮从头开始（来源属于回答，不属于会话）。上限
MAX_SOURCES 防止模型连抓几十页把消息撑大，超出的不再分配编号（仍可正常抓取）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_SOURCES = 20


@dataclass
class SourceEntry:
    index: int
    url: str
    title: str = ""
    kind: str = "web_fetch"


@dataclass
class SourceRegistry:
    max_sources: int = MAX_SOURCES
    _entries: list[SourceEntry] = field(default_factory=list)
    _by_url: dict[str, SourceEntry] = field(default_factory=dict)

    def register(self, url: str, title: str = "", kind: str = "web_fetch") -> int:
        """登记一个来源，返回轮内编号（1 起）；空 URL / 超上限返回 0（不编号）。"""
        url = (url or "").strip()
        if not url:
            return 0
        existing = self._by_url.get(url)
        if existing is not None:
            if title and not existing.title:
                existing.title = title.strip()[:200]
            return existing.index
        if len(self._entries) >= self.max_sources:
            return 0
        entry = SourceEntry(
            index=len(self._entries) + 1, url=url,
            title=(title or "").strip()[:200], kind=kind,
        )
        self._entries.append(entry)
        self._by_url[url] = entry
        return entry.index

    def label(self, url: str, title: str = "", kind: str = "web_fetch") -> str:
        """工具输出里用的前缀：``[来源 3] ``（未编号/无登记簿时为空串）。"""
        idx = self.register(url, title, kind)
        return f"[来源 {idx}] " if idx else ""

    def to_list(self) -> list[dict]:
        return [
            {"index": e.index, "url": e.url, "title": e.title, "kind": e.kind}
            for e in self._entries
        ]

    def __len__(self) -> int:
        return len(self._entries)


def register_source(ctx, url: str, title: str = "", kind: str = "web_fetch") -> str:
    """工具侧入口：登记并返回 ``[来源 N] `` 前缀（ctx 没有登记簿时返回空串）。

    ctx.sources 默认 None（单测/子代理直接构造 ToolContext 的路径），此时工具
    行为与加来源之前逐字节一致——来源是增强，不是工具的前置条件。
    """
    reg = getattr(ctx, "sources", None)
    if reg is None:
        return ""
    return reg.label(url, title, kind)
