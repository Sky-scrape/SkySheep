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
import datetime
import hashlib
import html.parser
import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import httpcore
import httpx
from pydantic import BaseModel, Field

from .. import obs
from ..instance import data_home
from ..sanitize import (
    UNTRUSTED_DATA_NOTE,
    scan_injection_patterns,
    untrusted_frame,
)
from ..textio import write_text_atomic
from .base import Safety, Tool, ToolContext, ToolError, truncate_output

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 20_000
TIMEOUT_S = 20.0
USER_AGENT = "SkySheep-web-fetch/0.4 (+open-source agent workbench)"

# NAT64/DNS64、6to4、Teredo：这些 IPv6 段内嵌着 IPv4 地址，经翻译网关可达
# 内网——`is_global` 认它们是公网（地址段本身注册在公网），但语义上等价于
# 访问被内嵌的那个 IPv4（如 64:ff9b::7f00:1 → 127.0.0.1）。显式排除（审查 C-1）。
_TRANSLATION_NETS = tuple(
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "2002::/16", "2001::/32")
)


class WebFetchArgs(BaseModel):
    # URL 总长上限（审查 P1-2）：web_fetch 是 READONLY 自动放行，不加约束的话
    # 模型可以把任意长的会话内容拼进 query 外带；正常网页 URL 远用不到这么长。
    url: str = Field(
        max_length=2048, description="要抓取的 http(s) 网页地址"
    )
    max_chars: int = Field(
        default=DEFAULT_MAX_CHARS, ge=200, le=100_000, description="返回文本的最大字符数"
    )


def _safe_charset(name: str | None) -> str:
    """对端声明的 charset 不可信：认不出来的一律回 utf-8。

    旧实现直接把它交给 bytes.decode——伪造的 charset（如 "x-nonexistent"）
    会让 LookupError 穿透成 500（安全审查低危项）。codecs.lookup 先校验；
    但只查名字不够——base64/hex/zlib_codec/bz2_codec/uu_codec/quopri_codec
    这类字节变换编解码器 lookup 同样成功，bytes.decode 却只认文本编码，照样
    抛 LookupError（"'base64' is not a text encoding"），整个抓取报工具错误
    （审查发现 10）。故再用 CodecInfo._is_text_encoding 挡一道：非文本编码
    一律回 utf-8（errors="replace" 兜底，页面内容不至于拿不到）。
    """
    if not name:
        return "utf-8"
    try:
        info = codecs.lookup(name)
    except (LookupError, ValueError):
        return "utf-8"
    # _is_text_encoding 是 CodecInfo 上 3.x 全系存在的私有标志位（文本编码 True、
    # 字节变换 False，实测 rot_13 也归 False）；万一上游移除，getattr 兜底 True
    # ——退回「只查名字」的旧口径，不会误伤任何真文本编码。
    if not getattr(info, "_is_text_encoding", True):
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
    # 畸形 URL 的 hostname 可能是 None / 空串（如 userinfo + IPv6 字面量剥凭据后
    # 再解析的形态）：显式拒绝而不是让它炸成 AttributeError（审查 P2-8）。
    host = str(host or "").strip()
    if not host:
        raise ToolError("web_fetch：URL 缺少主机名，无法定位目标")
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
        if isinstance(ip, ipaddress.IPv6Address):
            v4 = ip.ipv4_mapped
            if v4 is not None and not v4.is_global:
                raise ToolError(
                    f"web_fetch 拒绝 IPv4 映射地址（{host} → {ip}）：内嵌非公网 IPv4"
                )
            if any(ip in net for net in _TRANSLATION_NETS):
                raise ToolError(
                    f"web_fetch 拒绝翻译段地址（{host} → {ip}）："
                    "NAT64/6to4/Teredo 可翻译到内网 IPv4"
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


# 内容整体丢弃的标签（旧正则实现同一清单）。script/style 是 HTMLParser 的
# CDATA 元素：其内部只有对应的闭合标签会被当标签解析，其余内容原样走
# handle_data——正好交给 _skip_depth 整段丢弃。
_TEXT_DROP_TAGS = frozenset(("script", "style", "noscript", "svg", "head", "iframe"))
# 结束时补换行的块级标签（与旧正则同一清单）
_TEXT_BLOCK_TAGS = frozenset(
    ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
     "section", "article", "blockquote", "pre", "table")
)


class _HTMLTextExtractor(html.parser.HTMLParser):
    """线性时间 HTML → 纯文本（审查 P-6：替代旧的正则实现）。

    旧实现用 ``<tag>.*?</tag>`` 正则剥离 script 等块：对大量未闭合的开标签，
    每个起点都让惰性 ``.*?`` 扫到串尾，整体呈平方级回溯——256 KB 恶意页面
    要跑 40+ 秒，2 MB（web_fetch 的响应体上限）按曲线外推约 45 分钟。而
    web_fetch 是 READONLY 自动放行工具，同步调用发生在事件循环线程上，
    一次抓取就能把整个引擎（WS 推送、渠道、其他会话）全部卡死。HTMLParser
    是状态机、线性时间，对任意输入不会退化。

    与旧正则实现的行为对齐（对照测试逐形态钉住，见
    tests/test_web_fetch_hardening.py）：**每个**标签（含注释、DOCTYPE 等
    声明）都替换为一个空格分隔——行内标签不黏连（``<td>A</td><td>B</td>``
    → "A B" 而不是 "AB"）；丢弃名单内标签的**整个块**（内容实体不解码）
    替换为一个空格；``<br>`` 换行；块级标签闭合换行；实体解码；按行折叠空白。

    三处刻意优于旧正则、对照测试断言为「明确更优」，不得退回：
    - 属性值里的 ``>`` 不会截断标签（旧正则把 ``<a title="a>b">`` 撕成两半，
      属性残片 ``b">`` 当正文泄漏）；
    - 未闭合的丢弃标签使其后的内容一并跳过（浏览器对未闭合 script 的语义
      也是「其后的都是脚本文本」；旧正则匹配不到闭合标签会把脚本源码、
      title 元信息当正文吐给模型）——作为容错，丢弃途中遇到 ``<body>`` 视为
      head 等容器提前结束（浏览器对未闭合 head 同样隐式收口）；
    - 线性时间，对任意输入不平方级回溯。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0  # >0：正处于丢弃名单标签的内部

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth:
            if tag == "body":  # 容错：未闭合 head 的页面从 body 起恢复取文
                self._skip_depth = 0
                self._chunks.append(" ")  # <body> 标签本身在旧实现里也是一个空格
            elif tag in _TEXT_DROP_TAGS:
                self._skip_depth += 1
            return
        if tag in _TEXT_DROP_TAGS:
            self._skip_depth = 1  # 丢弃块的分隔空格由闭合处补（旧实现整块一个空格）
        elif tag == "br":
            self._chunks.append("\n")
        else:
            # 旧实现把每个标签替换为一个空格：行内/块级开始标签同样补分隔，
            # 否则 "Hello<p>World" 会黏成 "HelloWorld"（发现 8 的回归点）
            self._chunks.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_depth:
            if tag in _TEXT_DROP_TAGS:
                self._skip_depth -= 1
                if not self._skip_depth:
                    # 整个丢弃块（<script>…</script> 等）在旧实现里替换为
                    # 恰好一个空格：两侧文本靠它分隔
                    self._chunks.append(" ")
            return
        if tag in _TEXT_BLOCK_TAGS:
            self._chunks.append("\n")
        else:
            self._chunks.append(" ")  # 行内结束标签：与旧实现的「标签→空格」对齐

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    # 旧正则的 ``(?s)<[^>]+>`` 对注释 / DOCTYPE / 处理指令 / CDATA 段同样是
    # 「整段换一个空格」——这四个回调都是「非丢弃上下文里出现 → 补一个空格」。
    def handle_comment(self, data):
        if not self._skip_depth:
            self._chunks.append(" ")

    def handle_decl(self, decl):
        if not self._skip_depth:
            self._chunks.append(" ")

    def handle_pi(self, data):
        if not self._skip_depth:
            self._chunks.append(" ")

    def unknown_decl(self, data):
        if not self._skip_depth:
            self._chunks.append(" ")

    def text(self) -> str:
        return "".join(self._chunks)


def _install_pinned_backend(transport, backend, label: str) -> None:
    """把自研钉连后端装进 httpx transport（防 DNS rebinding 的关键一步）。

    ``transport._pool`` 是 httpx 的私有字段、``_network_backend`` 是 httpcore
    连接池的私有字段——都在公开 API 之外，上游升级可能改名。两种失效模式必须
    区分对待：

    - ``_pool`` 整个没了 → 旧代码会 AttributeError 响亮失败（fail closed），
      保持这一取向：装不上就拒绝发请求；
    - 仅 ``_network_backend`` 改名 → 普通属性赋值会**静默**变成死属性，
      钉连失效后整条 SSRF 防线只剩 getaddrinfo 那一次校验——这种静默退化
      必须显式 getattr 检查防住（审查 P-17）。

    版本锚点（2026-09，uv.lock 锁定）：httpx 0.28.1 / httpcore 1.0.9；
    pyproject 对 httpx 只约束 ``>=0.27`` 无上界，升级时先跑
    tests/test_web_fetch_hardening.py——钉连结构变化会让那里的端到端用例
    与本检查一起红。
    """
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError(
            f"{label}：当前 httpx/httpcore 版本的私有结构已变化，"
            "DNS 钉连（防 rebinding）无法安装；拒绝在不设防的情况下发请求"
            "（请按 tools/web.py 的版本锚点注释核对 httpx/httpcore 版本）"
        )
    pool._network_backend = backend


def html_to_text(html: str) -> str:
    """极简 HTML → 纯文本：去 script/style/head、块级标签闭合换行、解码实体。

    与旧正则实现对齐的骨架行为：**每个**标签（含注释/声明）替换为一个空格
    ——行内标签之间不黏连；丢弃名单内标签的整块内容替换为一个空格；``<br>``
    与块级标签闭合换行。三处刻意优于旧实现（属性含 >、未闭合 head/script、
    线性时间），详见 _HTMLTextExtractor 的说明与对照测试。

    用标准库 HTMLParser 线性解析：绝不用回溯型正则处理任意来源的 HTML
    ——调用方（web_fetch）无法控制页面内容。
    """
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in parser.text().splitlines())
    return "\n".join(ln for ln in lines if ln)


# ---- 提示注入纵深防御（第二期）：隔离区模式 + 站点信任评级 ----
# 一期（sanitize.untrusted_frame / scan_injection_patterns，「框起来 + 标记」）
# 保持并继续生效；二期只加两件事，全部只影响提示文案与日志，不改变放行行为。

# 隔离区模式开关（默认关）。将来配置化路径：config.py 增加安全配置项
# （形如 security.web_fetch_quarantine 的布尔项），经设置页读写后热生效并注入
# 此处；现阶段以模块常量承载，测试可直接翻转（monkeypatch），行为面与配置化后
# 完全一致。开启后 web_fetch 抓取正文不再整体进上下文：全文原子落盘隔离文件，
# 模型拿到的是来源、信任级、前 N 字符摘录与隔离文件路径——要全文须用文件读取
# 工具读取该路径（走权限门确认并留痕），注入内容不再随抓取静淌进上下文。
QUARANTINE_ENABLED = False
# 隔离区模式下返回给模型的正文摘录长度（字符）；全文在隔离文件里按需获取。
QUARANTINE_EXCERPT_CHARS = 600
# 站点信誉存储文件名（SKYSHEEP_HOME 下）：手动标记 + 注入命中自动计数。
REPUTATION_FILENAME = "web_reputation.json"

# 手动标记取值（信誉存储 JSON 里的 manual 字段）
MARK_GOOD = "good"
MARK_SUSPICIOUS = "suspicious"
# 信任级展示文案（untrusted_frame 头部与结构化日志用）
TRUST_GOOD = "已知良好"
TRUST_SUSPICIOUS = "已知可疑"
TRUST_UNKNOWN = "未知"


def _norm_host(host: str | None) -> str:
    return (host or "").strip().lower()


class SiteReputation:
    """站点信任评级存储：手动标记（已知良好/已知可疑）+ 自动计数（注入命中次数）。

    落 SKYSHEEP_HOME 下单个 JSON（引擎自有状态文件，textio.write_text_atomic
    原子写）；损坏（非 JSON / 结构不对 / 读不了）时兜底重建为空表并留一条
    warning 日志——评级只是标注，绝不能因为它让抓取失败。

    信任级判定（只影响提示文案与日志，不改变任何放行行为）：手动标记优先
    （已知良好 / 已知可疑），否则按自动计数给「曾报注入 N 次」，零命中为
    「未知」。每次 web_fetch 命中 scan_injection_patterns 记一次数。
    """

    VERSION = 1

    def __init__(self, path: Path | None = None) -> None:
        # path=None：首次使用时才按 SKYSHEEP_HOME 解析（导入期不吃环境变量，
        # 测试隔离友好）；显式传 path 供单元测试定点检查。
        self._path = path
        self._data: dict | None = None  # 惰性加载；None = 尚未读过盘

    # ---- 存取 ----

    def _file(self) -> Path:
        if self._path is None:
            self._path = data_home() / REPUTATION_FILENAME
        return self._path

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        path = self._file()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._data = {"version": self.VERSION, "hosts": {}}
            return self._data  # 还没有任何信誉记录：空表起家，不算损坏
        except (OSError, ValueError):
            raw = None
        if not (isinstance(raw, dict) and isinstance(raw.get("hosts"), dict)):
            # 走到这里必然是「文件存在但读不出合法结构」（坏 JSON / 结构不对；
            # 文件不存在已在上面提前返回）→ 告警并重建为空表
            obs.warning(
                "web_reputation_rebuild", "站点信誉存储损坏，已重建为空表",
                path=str(path),
            )
            raw = {"version": self.VERSION, "hosts": {}}
            self._data = self._normalize(raw)
            try:
                self._save()
            except OSError:
                pass  # 重建都写不进去也只能放弃：评级绝不能拖垮抓取
            return self._data
        self._data = self._normalize(raw)
        return self._data

    @staticmethod
    def _normalize(data: dict) -> dict:
        """逐条归一：坏条目按空处理，不让单个坏 host 拖垮整表。"""
        hosts: dict = {}
        for host, entry in (data.get("hosts") or {}).items():
            if not isinstance(entry, dict):
                continue
            manual = entry.get("manual")
            hits = entry.get("injection_hits")
            hosts[_norm_host(host)] = {
                "manual": manual if manual in (MARK_GOOD, MARK_SUSPICIOUS) else None,
                "injection_hits": hits if isinstance(hits, int) and hits >= 0 else 0,
            }
        return {"version": data.get("version") or SiteReputation.VERSION, "hosts": hosts}

    def _save(self) -> None:
        write_text_atomic(
            self._file(), json.dumps(self._data, ensure_ascii=False, indent=2) + "\n"
        )

    # ---- 查询与登记 ----

    def label(self, host: str | None) -> str:
        """站点信任级文案：已知良好 / 已知可疑 / 曾报注入 N 次 / 未知。"""
        entry = self._load()["hosts"].get(_norm_host(host))
        if not entry:
            return TRUST_UNKNOWN
        if entry["manual"] == MARK_GOOD:
            return TRUST_GOOD
        if entry["manual"] == MARK_SUSPICIOUS:
            return TRUST_SUSPICIOUS
        hits = entry["injection_hits"]
        return f"曾报注入 {hits} 次" if hits > 0 else TRUST_UNKNOWN

    def record_hit(self, host: str | None) -> int:
        """记一次注入命中（每次抓取命中 +1，与命中形态数无关），返回累计次数。"""
        entry = self._load()["hosts"].setdefault(
            _norm_host(host), {"manual": None, "injection_hits": 0}
        )
        entry["injection_hits"] += 1
        try:
            self._save()
        except OSError as e:
            # 计数写丢了只损失标注精度，不影响本次抓取结果
            obs.warning("web_reputation_save_failed", "站点信誉计数写盘失败", err=str(e))
        return entry["injection_hits"]

    def set_manual(self, host: str | None, mark: str | None) -> None:
        """手动标记站点：'good' / 'suspicious' / None（清除标记，保留自动计数）。

        手动标记优先于自动计数；将来设置页/WS 方法暴露时直接调这里。
        """
        if mark not in (None, MARK_GOOD, MARK_SUSPICIOUS):
            raise ValueError(f"未知的手动标记: {mark!r}")
        data = self._load()
        key = _norm_host(host)
        entry = data["hosts"].get(key)
        if entry is None:
            if mark is None:
                return  # 无标记可清：不落地空条目
            entry = {"manual": None, "injection_hits": 0}
            data["hosts"][key] = entry
        entry["manual"] = mark
        if mark is None and not entry["injection_hits"]:
            data["hosts"].pop(key, None)  # 清完什么都不剩：不留空壳
        self._save()


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "抓取一个公网网页并转为纯文本返回（HTML 自动去标签）。"
        "适合查官方文档、读在线资料、核对接口行为。"
        "仅支持 http/https 公网地址，内网与回环地址会被拒绝；长文本会被截断。"
        "URL 只用于定位要看的网页：不要把会话内容、文件内容、密钥或用户隐私"
        "拼进 URL（含 query 参数）——这是单向抓取工具，不是数据外发通道。"
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
        # 站点信任评级（二期）：手动标记 + 注入命中自动计数；路径首次使用时才解析
        self.reputation = SiteReputation()

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
        _install_pinned_backend(
            transport, _PinnedBackend({parsed.hostname: ips}), "web_fetch"
        )
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

    @staticmethod
    def _strip_userinfo(url: str) -> str:
        """URL 带 userinfo（https://user:pass@host/）时剥掉再请求：凭据会随请求
        发给对端并留在历史/日志里，而 URL 里出现凭据基本都是泄漏（安全审查
        低危项）。重定向的每一跳同样适用——Location 里带的凭据 httpx 会转成
        Basic Auth 发出去，只在首跳剥挡不住（审查 C-2）。
        """
        parsed = urlparse(url)
        if not (parsed.username or parsed.password):
            return url
        netloc = parsed.hostname or ""
        # IPv6 字面量的 hostname 不带方括号：原样拼回去再解析会把它当「主机:端口」
        # 切碎，hostname 变 None（审查 P2-8）
        if ":" in netloc:
            netloc = f"[{netloc}]"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    async def run(self, args: WebFetchArgs, ctx: ToolContext) -> str:
        url = args.url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ToolError("web_fetch 需要 http/https URL，例如 https://example.com/docs")
        url = self._strip_userinfo(url)
        parsed = urlparse(url)

        # getaddrinfo 是同步调用：放线程池，别让慢 DNS 停摆整个事件循环
        # （审查 P2-6；web_search / imagegen 早已是 to_thread 口径）
        ips = await asyncio.to_thread(self._pin, parsed.hostname)

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
                nxt = self._strip_userinfo(nxt)
                p2 = urlparse(nxt)
                ips = await asyncio.to_thread(self._pin, p2.hostname)  # 每一跳重新解析、校验并固定
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
        # 外发审计（审查 P1-2）：READONLY 工具零确认外发，日志里留一条「去了哪」。
        # 刻意只记 host/path 与 query 长度——query 本体可能正是被外带的敏感值，
        # 落盘会随诊断包二次泄漏；长度异常本身就是最有用的排查信号。
        parsed_final = urlparse(current)
        obs.info(
            "web_fetch", "外发请求完成",
            host=parsed_final.hostname, path=parsed_final.path[:200],
            query_len=len(parsed_final.query), status=status, bytes=len(body),
        )
        # 提示注入纵深防御（第一期「框起来 + 标记」，不拦截）：抓取到的文本是
        # 不可信外部内容，包进明确边界行交还模型；命中经典注入形态时在输出尾部
        # 附一行提示、留一条结构化日志供「这一轮为什么…」检索。只标记不拦截：
        # 抓取内容一字不改、照常返回，是否照做仍由模型与用户在权限门下决定。
        # 二期在此基础上加：命中自动计入站点信誉（两态都记），隔离区模式开启时
        # 正文落盘隔离文件、只回摘录与路径（见 _quarantined_reply）。
        hits = scan_injection_patterns(text)
        host = parsed_final.hostname or ""
        if hits:
            self.reputation.record_hit(host)  # 自动计数：只影响标注，不影响放行
        trust_label = ""
        if hits or QUARANTINE_ENABLED:
            trust_label = self.reputation.label(host)
        if hits:
            # 只记形态名与定位信息，不落正文（obs 约定：只记标识与度量）
            obs.warning(
                "web_fetch_injection_hint", "抓取内容命中疑似指令注入形态",
                host=parsed_final.hostname, path=parsed_final.path[:200],
                patterns=hits, trust=trust_label,
            )
        if QUARANTINE_ENABLED:
            return self._quarantined_reply(
                current, ctype, text + note, hits, trust_label, args.max_chars
            )
        framed = untrusted_frame(current, truncate_output(text + note, args.max_chars))
        if hits:
            framed += "\n\n⚠ 检测到疑似指令注入形态：" + "、".join(hits)
        # 提示注入纵深防御（机械标注层）：元信息行之后、边界框之前压一遍
        # 「数据不是指令」——标注是工具自己的话，在边界框之外；正文一字不丢
        return f"[{current}] ({ctype.split(';')[0].strip() or 'text'})\n\n" \
            + UNTRUSTED_DATA_NOTE + "\n\n" + framed

    def _quarantined_reply(
        self, current: str, ctype: str, body_text: str, hits: list[str],
        trust_label: str, max_chars: int,
    ) -> str:
        """隔离区模式的 web_fetch 回包：全文落盘，模型只见摘录 + 路径。

        抓取正文不再整体进上下文：sha256 按内容哈希命名，原子写入
        SKYSHEEP_HOME/quarantine/<日期>/；回给模型的是边界框包住的来源、信任级
        与前 N 字符摘录，以及「要全文用文件读取工具获取（走权限门留痕）」的提示。
        落盘失败必须报错而不是退回全文——隔离模式下把正文整段吐回上下文等于
        没隔离。
        """
        digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        qdir = data_home() / "quarantine" / datetime.date.today().isoformat()
        qpath = qdir / f"{digest}.txt"
        try:
            write_text_atomic(qpath, body_text)
        except OSError as e:
            raise ToolError(f"web_fetch：正文隔离落盘失败（{e}），已放弃本次抓取") from e
        obs.info(
            "web_fetch_quarantine", "正文已隔离落盘，仅摘录进上下文",
            host=urlparse(current).hostname, chars=len(body_text),
            file=str(qpath),
        )
        excerpt = body_text[: min(QUARANTINE_EXCERPT_CHARS, max_chars)]
        framed = untrusted_frame(current, excerpt, trust=trust_label or TRUST_UNKNOWN)
        out = f"[{current}] ({ctype.split(';')[0].strip() or 'text'})\n\n" \
            + UNTRUSTED_DATA_NOTE + "\n\n" + framed
        tail = (
            f"\n\n（隔离区：正文共 {len(body_text)} 字符，上方仅前 {len(excerpt)} 字符摘录，"
            "其余部分未进入上下文。\n"
            f"全文已存入隔离文件：{qpath}\n"
            "需要全文时，用文件读取工具读取上述路径获取（读取会经权限门确认并留痕）。）"
        )
        if hits:
            tail += "\n\n⚠ 检测到疑似指令注入形态：" + "、".join(hits)
        return out + tail


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
