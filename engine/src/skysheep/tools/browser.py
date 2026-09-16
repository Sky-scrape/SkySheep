"""浏览器控制工具：browser（open 打开网址 / search 搜索）。

Agent 此前只有 web_fetch（把网页抓成纯文本给自己看），没有「把网页展示给用户」
的能力——「帮我打开这个链接」「搜一下 XX 然后你自己看」这类诉求只能干瞪眼。
browser 工具用系统默认浏览器打开 URL（用户自己的浏览器、自己的会话与配置文件，
不存在本机抓取，因此 web_fetch 那套 SSRF 防线在这里不适用；但仍然只放行
http(s)，file:// / javascript: 等一律拒绝）。

安全模型与电脑控制同档：DANGEROUS 逐次确认，「总是允许」按动作前缀生成
（arg_text 首词是 action，白名单粒度即 open / search 单动作）。
Windows 走 ctypes ShellExecuteW（与 support 同一路数，不经命令行）；其他平台
webbrowser 兜底（--browser 模式跨平台可用）。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError

SEARCH_ENGINES = {
    "bing": "https://www.bing.com/search?q=",
    "baidu": "https://www.baidu.com/s?wd=",
    "duckduckgo": "https://duckduckgo.com/?q=",
}


def validate_url(raw: str) -> str:
    """仅放行 http(s) 绝对地址；返回规范化后的字符串，非法抛 ToolError。"""
    url = str(raw or "").strip()
    if not url:
        raise ToolError("open 动作需要提供 url 参数（http/https 地址）。")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError(
            f"只允许打开 http(s) 网址，收到的是「{url[:80]}」。"
            "本地文件请直接用 read_file / read_document 读取，不需要开浏览器。"
        )
    return url


def build_search_url(engine: str, query: str) -> str:
    base = SEARCH_ENGINES.get(engine)
    if base is None:
        raise ToolError(
            "不支持的搜索引擎：" + engine + "（可选：" + " / ".join(SEARCH_ENGINES) + "）"
        )
    return base + quote(query)


def _open_in_browser(url: str) -> None:
    if sys.platform == "win32":
        from .. import support

        support.shell_open(url)  # ShellExecuteW 的 lpFile 天然接受 URL
    else:
        import webbrowser

        webbrowser.open(url)


class BrowserArgs(BaseModel):
    action: Literal["open", "search"] = Field(description="open=打开网址；search=搜索引擎搜索")
    url: str = Field(default="", description="open 时的 http(s) 网址")
    query: str = Field(default="", description="search 时的搜索关键词")
    engine: Literal["bing", "baidu", "duckduckgo"] = Field(
        default="bing", description="search 使用的搜索引擎（默认 bing，国内可直连）"
    )


class BrowserTool(Tool):
    name = "browser"
    description = (
        "控制浏览器：用系统默认浏览器打开网址（open）或搜索关键词（search）。"
        "适合把网页/搜索结果展示给用户看的场景；需要自己读网页内容时用 web_fetch。"
        "会在用户的真实浏览器里打开页面，高危操作，会先请求用户确认。"
    )
    safety = Safety.DANGEROUS
    args_model = BrowserArgs

    def arg_text(self, input_dict: dict) -> str:
        # 首词是 action：「总是允许」的前缀白名单粒度即单动作（open / search）
        action = str(input_dict.get("action", ""))
        target = input_dict.get("url") or input_dict.get("query") or ""
        return f"{action} {str(target)[:80]}".strip()

    async def run(self, args: BrowserArgs, ctx: ToolContext) -> str:
        if args.action == "open":
            url = validate_url(args.url)
        else:
            if not args.query.strip():
                raise ToolError("search 动作需要提供 query 参数（搜索关键词）。")
            url = build_search_url(args.engine, args.query.strip())
        try:
            await asyncio.to_thread(_open_in_browser, url)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"打开浏览器失败：{e}") from None
        if args.action == "search":
            return f"已在系统默认浏览器中搜索「{args.query.strip()}」（{args.engine}）：{url}\n" \
                "页面已在用户的浏览器里打开，你看不到页面内容；需要内容时用 web_fetch 抓取。"
        return f"已在系统默认浏览器打开：{url}\n" \
            "页面已在用户的浏览器里打开，你看不到页面内容；需要内容时用 web_fetch 抓取。"
