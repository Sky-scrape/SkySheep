"""web_fetch 工具：抓取公网网页并转为纯文本（对标 Claude Code WebFetch /
Codex 联网检索，SkySheep 由此获得联网能力）。

安全边界（Agent 联网工具的标准做法，非可选项）：
- 仅接受 http/https URL；
- 解析域名得到的 IP 必须是公网地址——拒绝回环/内网/链路本地/保留段（防 SSRF）；
- 重定向逐跳重新做协议与地址校验（最多 5 跳）；
- 响应体限 2 MB、超时 20s；HTML 去标签转纯文本后按字符数截断，防撑爆上下文。

只读工具（Safety.READONLY）：规划模式下也可用，方便先联网调研再出计划。

测试用本地 HTTP 桩时构造 WebFetchTool(allow_private_hosts=True) 放开内网限制；
正式交付的工具对象一律保持默认 False。
"""

from __future__ import annotations

import asyncio
import html as html_mod
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError, truncate_output

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 20_000
TIMEOUT_S = 20.0
USER_AGENT = "SkySheep-web-fetch/0.4 (+open-source agent workbench)"


class WebFetchArgs(BaseModel):
    url: str = Field(description="要抓取的 http(s) 网页地址")
    max_chars: int = Field(
        default=DEFAULT_MAX_CHARS, ge=200, le=100_000, description="返回文本的最大字符数"
    )


def _assert_public_host(host: str) -> None:
    """域名必须解析到公网 IP，否则拒绝（防 SSRF）。"""
    if host.lower() in ("localhost", "0.0.0.0"):
        raise ToolError(f"web_fetch 拒绝内网地址: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ToolError(f"无法解析主机 {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError(
                f"web_fetch 拒绝非公网地址（{host} → {ip}）：内网/回环地址不允许访问"
            )


def html_to_text(html: str) -> str:
    """极简 HTML → 纯文本：去 script/style/head、块级标签换行、去其余标签、解码实体。"""
    html = re.sub(r"(?is)<(script|style|noscript|svg|head|iframe)[^>]*>.*?</\1\s*>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|blockquote|pre|table)>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    text = html_mod.unescape(html)
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "抓取一个公网网页并转为纯文本返回（HTML 自动去标签）。"
        "适合查官方文档、读在线资料、核对接口行为。"
        "仅支持 http/https 公网地址，内网与回环地址会被拒绝；长文本会被截断。"
    )
    safety = Safety.READONLY
    args_model = WebFetchArgs

    def __init__(self, allow_private_hosts: bool = False) -> None:
        # 仅测试注入：允许访问内网（本地 HTTP 桩）
        self.allow_private_hosts = allow_private_hosts

    async def run(self, args: WebFetchArgs, ctx: ToolContext) -> str:
        url = args.url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ToolError("web_fetch 需要 http/https URL，例如 https://example.com/docs")
        if not self.allow_private_hosts:
            _assert_public_host(parsed.hostname)

        current = url
        resp: httpx.Response | None = None
        for _ in range(MAX_REDIRECTS + 1):
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=TIMEOUT_S,
                trust_env=False,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                resp = await client.get(current)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if not location:
                    raise ToolError(f"重定向缺少 Location 头: {current}")
                nxt = urljoin(current, location)
                p2 = urlparse(nxt)
                if p2.scheme not in ("http", "https") or not p2.hostname:
                    raise ToolError(f"重定向到不支持的协议: {nxt}")
                if not self.allow_private_hosts:
                    _assert_public_host(p2.hostname)
                current = nxt
                continue
            break
        else:
            raise ToolError(f"重定向次数超过 {MAX_REDIRECTS} 次: {url}")

        assert resp is not None
        if resp.status_code >= 400:
            raise ToolError(f"HTTP {resp.status_code}: {current}")
        ctype = resp.headers.get("content-type", "")
        body = resp.content[:MAX_RESPONSE_BYTES]
        if "html" in ctype.lower() or b"<html" in body[:600].lower():
            text = html_to_text(body.decode(resp.encoding or "utf-8", errors="replace"))
        else:
            text = body.decode("utf-8", errors="replace")
        if not text.strip():
            raise ToolError(f"页面没有可提取的文本内容（content-type: {ctype or '未知'}）")
        return f"[{current}] ({ctype.split(';')[0].strip() or 'text'})\n\n" + truncate_output(
            text, args.max_chars
        )


# ---- web_search：联网搜索（先搜到链接，再用 web_fetch 读全文） ----
# 支持博查（国内直连）、Tavily、智谱 web-search 三家；「自动」档按此顺序取
# 第一个配好 Key 的服务。智谱档复用模型服务里已配置的 Zhipu API Key，零额外配置。

SEARCH_TIMEOUT_S = 15.0
MAX_SNIPPET_CHARS = 300
MAX_SEARCH_OUTPUT_CHARS = 8000


class WebSearchArgs(BaseModel):
    query: str = Field(description="搜索关键词（可用空格分隔多个词，中文英文均可）")
    max_results: int = Field(default=6, ge=1, le=10, description="返回结果条数")


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索：返回与查询相关的网页列表（标题 + 链接 + 摘要）。"
        "需要查资料、找文档、核实时效性信息时先用它，再用 web_fetch 读感兴趣的页面全文。"
    )
    safety = Safety.READONLY
    args_model = WebSearchArgs

    def __init__(self, provider: str = "", api_key: str = "", transport=None) -> None:
        self.provider = provider  # bocha / tavily / zhipu；空 = 未配置
        self.api_key = api_key
        self._transport = transport  # 仅测试注入

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.api_key)

    async def run(self, args: WebSearchArgs, ctx: ToolContext) -> str:
        if not self.configured:
            raise ToolError(
                "联网搜索未配置：请到 设置 · 技能与工具 · 联网搜索 选择服务商并填入 API Key"
                "（博查 bocha.cn / Tavily / 智谱任一；智谱会自动复用已配置的 Zhipu Key），"
                "或者直接用 web_fetch 抓取已知网址。"
            )
        query = args.query.strip()
        if not query:
            raise ToolError("搜索词不能为空")
        try:
            results = await asyncio.to_thread(self._search_sync, query, args.max_results)
        except httpx.HTTPError as e:
            raise ToolError(f"搜索服务请求失败：{type(e).__name__}: {e}") from e
        if not results:
            return f'[web_search] "{query}" 没有搜到相关结果，换个更具体的关键词试试。'
        lines = [f'[web_search] "{query}" — {len(results)} 条结果（{self.provider}）']
        for i, r in enumerate(results, start=1):
            snippet = (r.get("snippet") or "")[:MAX_SNIPPET_CHARS]
            lines.append(f"\n{i}. {r.get('title') or '(无标题)'}\n   {r.get('url') or ''}")
            if snippet:
                lines.append(f"   {snippet}")
        return truncate_output("\n".join(lines), MAX_SEARCH_OUTPUT_CHARS)

    # ---- 各服务商请求与响应解析（同步小函数，线程里跑） ----

    def _search_sync(self, query: str, max_results: int) -> list[dict]:
        if self.provider == "bocha":
            payload = {"query": query, "count": max_results, "summary": True}
            headers = {"Authorization": "Bearer " + self.api_key}
            url = "https://api.bochaai.com/v1/web-search"
        elif self.provider == "tavily":
            payload = {
                "query": query, "max_results": max_results,
                "search_depth": "basic", "include_answer": False,
            }
            headers = {"Authorization": "Bearer " + self.api_key}
            url = "https://api.tavily.com/search"
        elif self.provider == "zhipu":
            payload = {
                "search_engine": "search_std", "search_query": query, "count": max_results,
            }
            headers = {"Authorization": "Bearer " + self.api_key}
            url = "https://open.bigmodel.cn/api/paas/v4/web_search"
        else:
            raise ToolError(f"未知的搜索服务商: {self.provider}")

        with httpx.Client(timeout=SEARCH_TIMEOUT_S, trust_env=False, transport=self._transport) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            detail = resp.text[:200]
            raise ToolError(f"搜索服务返回 HTTP {resp.status_code}: {detail}")
        data = resp.json()
        return self._parse_results(data)

    def _parse_results(self, data: dict) -> list[dict]:
        out: list[dict] = []
        if self.provider == "bocha":
            pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
            for p in pages:
                out.append({
                    "title": p.get("name") or "",
                    "url": p.get("url") or "",
                    "snippet": p.get("summary") or p.get("snippet") or "",
                })
        elif self.provider == "tavily":
            for p in data.get("results") or []:
                out.append({
                    "title": p.get("title") or "",
                    "url": p.get("url") or "",
                    "snippet": p.get("content") or "",
                })
        elif self.provider == "zhipu":
            for p in data.get("search_result") or []:
                out.append({
                    "title": p.get("title") or "",
                    "url": p.get("link") or p.get("url") or "",
                    "snippet": p.get("content") or "",
                })
        return [r for r in out if r["url"]]
