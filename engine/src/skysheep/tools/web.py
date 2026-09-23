"""web_fetch 工具：抓取公网网页并转为纯文本（对标 Claude Code WebFetch /
Codex 联网检索，SkySheep 由此获得联网能力）。

安全边界（Agent 联网工具的标准做法，非可选项）：
- 仅接受 http/https URL；
- 解析域名得到的 IP 必须是公网地址——拒绝回环/内网/链路本地/保留段（防 SSRF）；
- 解析一次即把连接**固定**到已校验的 IP（防 DNS rebinding：校验与建连之间不再二次解析）；
- 重定向逐跳重新做协议、地址校验与固定（最多 5 跳）；
- 响应体**流式**读取并累计计数，超过 2 MB 立即中止（不等整个文件下完再截断）；
- 超时 20s；HTML 去标签转纯文本后按字符数截断，防撑爆上下文。

只读工具（Safety.READONLY）：规划模式下也可用，方便先联网调研再出计划。

测试用本地 HTTP 桩时构造 WebFetchTool(allow_private_hosts=True) 放开内网限制；
正式交付的工具对象一律保持默认 False。
"""

from __future__ import annotations

import asyncio
import codecs
import html as html_mod
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse, urlunparse

import httpcore
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


def _safe_charset(name: str | None) -> str:
    """对端声明的 charset 不可信：认不出来的一律回 utf-8。

    旧实现直接把它交给 bytes.decode——伪造的 charset（如 "x-nonexistent"）
    会让 LookupError 穿透成 500（安全审查低危项）。codecs.lookup 先校验。
    """
    if not name:
        return "utf-8"
    try:
        codecs.lookup(name)
    except (LookupError, ValueError):
        return "utf-8"
    return name


def _assert_public_host(host: str) -> None:
    """域名必须解析到公网 IP，否则拒绝（防 SSRF）。

    保留为薄封装：调用方（如 imagegen 的图片下载）只需要「先校验一次」时仍可用它；
    需要把连接固定到已校验 IP 的场景（web_fetch）请用 _resolve_public_ips
    拿到 IP 列表后自己建连。
    """
    _resolve_public_ips(host)


def _resolve_public_ips(host: str) -> list[str]:
    """解析主机并校验**全部**结果为公网地址，返回可用的 IP 列表（防 SSRF）。

    返回已解析并通过校验的 IP，供建连阶段直接复用（配合 _PinnedBackend 防
    DNS rebinding）。任何一个结果落在非公网段都直接拒绝：一个域名同时答出
    公网与内网地址时，不能只挑好听的用。
    """
    if host.lower() in ("localhost", "0.0.0.0", "::", "[::]"):
        raise ToolError(f"web_fetch 拒绝内网地址: {host}")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ToolError(f"无法解析主机 {host}: {e}") from e
    ips: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise ToolError(f"解析结果不是合法 IP（{host} → {raw}）") from None
        if not ip.is_global:
            raise ToolError(
                f"web_fetch 拒绝非公网地址（{host} → {ip}）：内网/回环地址不允许访问"
            )
        text = str(ip)
        if text not in ips:
            ips.append(text)
    if not ips:
        raise ToolError(f"无法解析主机 {host}")
    return ips


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """把指定主机的 TCP 连接固定到已校验的 IP 列表（防 DNS rebinding / TOCTOU）。

    背景：只做「先 getaddrinfo 校验、再交给 httpx 连接」是不够的——httpx 建连时会
    自己再解析一次 DNS，两次解析之间是攻击窗口：攻击者控制的域名把 TTL 设得很低，
    校验那一次答公网 IP 通过检查，建连那一次改答 127.0.0.1 或云 metadata 地址。
    这里在校验之后接手网络层：命中被固定的主机名时直接连已校验的 IP，不再走 DNS。

    Host 头与 TLS SNI 仍由 httpcore 用 URL 里的原主机名生成（见 AsyncHTTPConnection
    的 server_hostname），所以虚拟主机与证书校验照常生效——固定的只是「连到哪台机器」。
    """

    def __init__(self, pinned: dict[str, list[str]]) -> None:
        self._pinned = {h.lower(): ips for h, ips in pinned.items()}
        # AnyIOBackend 是 httpcore 公开导出的后端（httpx 的 AutoBackend 在 asyncio 下
        # 最终也是它）；SkySheep 全栈 asyncio，不存在 trio 分支。
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        targets = self._pinned.get(host.lower())
        if not targets:
            return await self._inner.connect_tcp(
                host, port, timeout=timeout, local_address=local_address,
                socket_options=socket_options,
            )
        last: Exception | None = None
        for ip in targets:  # 多个 IP 依次尝试，首个能连上的即用
            try:
                return await self._inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        raise last if last is not None else httpcore.ConnectError(
            f"没有可用的已校验地址: {host}"
        )

    async def connect_unix_socket(self, *args, **kwargs):
        return await self._inner.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


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
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = True
    args_model = WebFetchArgs

    def __init__(self, allow_private_hosts: bool = False) -> None:
        # 仅测试注入：允许访问内网（本地 HTTP 桩）
        self.allow_private_hosts = allow_private_hosts

    def _pin(self, host: str) -> list[str]:
        """解析并校验主机，返回可直接建连的 IP 列表。

        allow_private_hosts 打开时（测跱桩）直接解析但不做公网校验。
        """
        if self.allow_private_hosts:
            try:
                infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            except OSError as e:
                raise ToolError(f"无法解析主机 {host}: {e}") from e
            out: list[str] = []
            for info in infos:
                text = str(ipaddress.ip_address(info[4][0]))
                if text not in out:
                    out.append(text)
            if not out:
                raise ToolError(f"无法解析主机 {host}")
            return out
        return _resolve_public_ips(host)

    async def _fetch_once(self, url: str, parsed, ips: list[str]) -> tuple[int, dict, bytes]:
        """发一次请求，流式读取响应体并累计计数，超上限立即中止。

        返回 (状态码, 响应头, 已读字节)。非流式调用（``client.get``）会先把整个响应体
        读进内存再截断：目标返回没有 Content-Length 的巨大/无限流时，内存会在截断
        之前就被吃干。这里改成边读边计数，超限直接停止。
        """
        transport = httpx.AsyncHTTPTransport(trust_env=False)
        # 把连接固定到已校验的 IP：httpx 建连时不会再做一次 DNS 解析
        transport._pool._network_backend = _PinnedBackend({parsed.hostname: ips})
        buffer = bytearray()
        truncated = False
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                status = resp.status_code
                headers = dict(resp.headers)
                charset = _safe_charset(resp.charset_encoding)
                if status in (301, 302, 303, 307, 308):
                    return status, headers, b""  # 重定向体无意义，不读
                async for chunk in resp.aiter_bytes():
                    room = MAX_RESPONSE_BYTES - len(buffer)
                    if room <= 0:
                        truncated = True
                        break
                    if len(chunk) > room:
                        buffer += chunk[:room]
                        truncated = True
                        break
                    buffer += chunk
        if truncated:
            headers["x-skysheep-truncated"] = "1"
        headers["x-skysheep-charset"] = charset
        return status, headers, bytes(buffer)

    async def run(self, args: WebFetchArgs, ctx: ToolContext) -> str:
        url = args.url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ToolError("web_fetch 需要 http/https URL，例如 https://example.com/docs")
        # URL 里带 userinfo（https://user:pass@host/）时剥掉再请求：凭据会随请求
        # 发给对端并留在历史/日志里，而模型生成的 URL 里出现凭据基本都是泄漏
        # （安全审查低危项）
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            url = urlunparse(parsed._replace(netloc=netloc))
            parsed = urlparse(url)

        ips = self._pin(parsed.hostname)

        current = url
        status = 0
        headers: dict = {}
        body = b""
        for _ in range(MAX_REDIRECTS + 1):
            status, headers, body = await self._fetch_once(current, urlparse(current), ips)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location", "")
                if not location:
                    raise ToolError(f"重定向缺少 Location 头: {current}")
                nxt = urljoin(current, location)
                p2 = urlparse(nxt)
                if p2.scheme not in ("http", "https") or not p2.hostname:
                    raise ToolError(f"重定向到不支持的协议: {nxt}")
                ips = self._pin(p2.hostname)  # 每一跳重新解析、校验并固定
                current = nxt
                continue
            break
        else:
            raise ToolError(f"重定向次数超过 {MAX_REDIRECTS} 次: {url}")

        if status >= 400:
            raise ToolError(f"HTTP {status}: {current}")
        ctype = headers.get("content-type", "")
        charset = _safe_charset(headers.get("x-skysheep-charset"))
        if "html" in ctype.lower() or b"<html" in body[:600].lower():
            text = html_to_text(body.decode(charset, errors="replace"))
        else:
            text = body.decode("utf-8", errors="replace")
        if not text.strip():
            raise ToolError(f"页面没有可提取的文本内容（content-type: {ctype or '未知'}）")
        note = ""
        if headers.get("x-skysheep-truncated"):
            note = f"\n\n... [响应体超过 {MAX_RESPONSE_BYTES // 1024 // 1024} MB，已截断] ..."
        return f"[{current}] ({ctype.split(';')[0].strip() or 'text'})\n\n" + truncate_output(
            text + note, args.max_chars
        )


# ---- web_search：联网搜索（先搜到链接，再用 web_fetch 读全文） ----
# 支持博查（国内直连）、Tavily、智谱 web-search 三家；「自动」档按此顺序取
# 第一个配好 Key 的服务。智谱档复用模型服务里已配置的 Zhipu API Key，零额外配置。
# 「自定义」档指向用户自建的服务：SearXNG 走 GET（无需 Key），其它 REST 接口走
# POST，按 base_url 形态自动识别，一份配置两种协议。

SEARCH_TIMEOUT_S = 15.0
MAX_SNIPPET_CHARS = 300
MAX_SEARCH_OUTPUT_CHARS = 8000


class WebSearchArgs(BaseModel):
    query: str = Field(description="搜索关键词（可用空格分隔多个词，中文英文均可）")
    max_results: int = Field(default=6, ge=1, le=10, description="返回结果条数")


class _CustomProtocolMismatch(Exception):
    """自定义搜索的协议猜错了（如把 SearXNG 当 POST 接口调）。

    携带原始错误，两种协议都失败时把它报给用户，而不是报「都失败了」。
    """

    def __init__(self, original: ToolError) -> None:
        super().__init__(str(original))
        self.original = original


def _looks_like_searxng(base_url: str) -> bool:
    """判断自定义地址是否应先按 SearXNG 的 GET 协议试（决定尝试顺序）。

    两个依据：
    - 地址里出现 searx / format=json——用户直接粘了实例地址；
    - 地址只有主机没有路径（`http://localhost:8080`）——这正是自建 SearXNG 的
      典型写法（JSON 输出必须带 format=json，会被拼到 /search 后），而通用 REST
      接口几乎总要带自己的路径。

    误判的代价很低：两种协议本就会互备重试。
    """
    low = base_url.lower()
    if "searx" in low or "format=json" in low:
        return True
    try:
        return urlparse(base_url).path.strip("/") == ""
    except ValueError:
        return False


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索：返回与查询相关的网页列表（标题 + 链接 + 摘要）。"
        "需要查资料、找文档、核实时效性信息时先用它，再用 web_fetch 读感兴趣的页面全文。"
    )
    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = True
    args_model = WebSearchArgs

    def __init__(self, provider: str = "", api_key: str = "",
                 base_url: str = "", transport=None) -> None:
        self.provider = provider  # bocha / tavily / zhipu / custom；空 = 未配置
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self._transport = transport  # 仅测试注入

    @property
    def configured(self) -> bool:
        if self.provider == "custom":
            return bool(self.base_url)  # 自建服务可以不带 Key
        return bool(self.provider and self.api_key)

    async def run(self, args: WebSearchArgs, ctx: ToolContext) -> str:
        if not self.configured:
            raise ToolError(
                "联网搜索未配置：请到 设置 · 技能与工具 · 联网搜索 选择服务商并填入 API Key"
                "（博查 bocha.cn / Tavily / 智谱任一；智谱会自动复用已配置的 Zhipu Key），"
                "或者选「自定义」填入自建搜索服务地址（如 SearXNG），"
                "也可以直接用 web_fetch 抓取已知网址。"
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
        if self.provider == "custom":
            return self._search_custom_sync(query, max_results)
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

    # ---- 自定义服务：一份配置跑两种协议（SearXNG GET / 通用 REST POST） ----

    def _search_custom_sync(self, query: str, max_results: int) -> list[dict]:
        """自定义搜索：按 base_url 形态选协议，另一种不合适时自动互备。

        自建 SearXNG 用 GET /search?format=json 且通常无需鉴权；其它 REST 接口按
        POST JSON 调用。地址形态只是默认猜测，所以两边都失败时再试另一种，避免
        用户必须搞清楚自己那套服务到底算哪一类。

        这里不对地址做公网校验：自建 SearXNG 常跑在 localhost / 内网，这正是该档
        的典型用法；地址只能由用户在设置页写入，模型无法通过工具参数指定它，
        与 web_fetch 可被模型任意指定 URL 的风险面不同。
        """
        prefer_get = _looks_like_searxng(self.base_url)
        attempts = (self._custom_get, self._custom_post) if prefer_get \
            else (self._custom_post, self._custom_get)
        last_error: ToolError | None = None
        for attempt in attempts:
            try:
                results = attempt(query, max_results)
            except _CustomProtocolMismatch as e:
                last_error = e.original
                continue
            if results:
                return results[:max_results]
            return []  # 协议对上了、确实没结果：不再换协议重试
        raise last_error or ToolError("自定义搜索服务没有返回可用结果")

    def _custom_get(self, query: str, max_results: int) -> list[dict]:
        base = self.base_url
        low = base.lower()
        # SearXNG 的 JSON 输出靠 format=json 触发，否则回的是 HTML 页面。
        url = base if "format=json" in low else (
            base if low.endswith("/search") else base + "/search"
        )
        params = {"q": query}
        if "format=json" not in low:
            params["format"] = "json"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        with httpx.Client(timeout=SEARCH_TIMEOUT_S, trust_env=False,
                          transport=self._transport) as client:
            resp = client.get(url, params=params, headers=headers)
        if resp.status_code in (404, 405):
            raise _CustomProtocolMismatch(
                ToolError(f"自定义搜索服务返回 HTTP {resp.status_code}"))
        if resp.status_code >= 400:
            raise ToolError(f"搜索服务返回 HTTP {resp.status_code}: {resp.text[:200]}")
        return self._parse_loose(self._json_or_mismatch(resp))

    def _custom_post(self, query: str, max_results: int) -> list[dict]:
        if not self.base_url:
            raise ToolError("自定义搜索需要填写接口地址")
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        with httpx.Client(timeout=SEARCH_TIMEOUT_S, trust_env=False,
                          transport=self._transport) as client:
            resp = client.post(
                self.base_url,
                json={"query": query, "max_results": max_results},
                headers=headers,
            )
        if resp.status_code in (404, 405):
            raise _CustomProtocolMismatch(
                ToolError(f"自定义搜索服务返回 HTTP {resp.status_code}"))
        if resp.status_code >= 400:
            raise ToolError(f"搜索服务返回 HTTP {resp.status_code}: {resp.text[:200]}")
        return self._parse_loose(self._json_or_mismatch(resp))

    def _json_or_mismatch(self, resp: httpx.Response) -> object:
        """响应不是 JSON 就当协议猜错了（最常见的实例：SearXNG 的 POST /search
        会返回 HTML 页面），交给调用方换另一种协议重试。
        """
        try:
            return resp.json()
        except ValueError as e:
            raise _CustomProtocolMismatch(
                ToolError(f"自定义搜索服务返回的不是 JSON（{type(e).__name__}），"
                          f"响应开头：{resp.text[:120]}")) from e

    def _parse_loose(self, data) -> list[dict]:
        """宽容解析：常见搜索接口的结果结构各异，逐一尝试并归一。

        覆盖 SearXNG 的 results[]、博查式 data.webPages.value[]、智谱式
        search_result[]、以及 data.results[] / 裸数组等变体，把标题/链接/摘要
        三个字段拼出来；解析不到结构就当作 0 条结果，不报错（大多数情况是服务
        确实没结果，而不是接口写错）。
        """
        rows: list = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            nested: list = []
            for outer in (data.get("data"), data.get("webPages")):
                if isinstance(outer, dict):
                    nested.append((outer.get("webPages") or {}).get("value"))
                    nested.append(outer.get("value"))
            candidates = [
                data.get("results"),
                data.get("search_result"),
                data.get("data"),
                *nested,
            ]
            for c in candidates:
                if isinstance(c, list) and c and isinstance(c[0], dict):
                    rows = c
                    break
                if isinstance(c, dict):
                    inner = c.get("results") or c.get("value")
                    if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                        rows = inner
                        break
        out: list[dict] = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            out.append({
                "title": p.get("title") or p.get("name") or "",
                "url": p.get("url") or p.get("link") or p.get("href") or "",
                "snippet": p.get("content") or p.get("snippet") or p.get("summary")
                           or p.get("description") or "",
            })
        return [r for r in out if r["url"]]

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
