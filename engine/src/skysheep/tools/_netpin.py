"""公网 IP 校验与 HTTP TCP DNS 钉连后端（tools/web、tools/imagegen 等共用）。

将 SSRF 防线（公网解析校验、NAT64/6to4/Teredo 过滤、httpcore 异步/同步
DNS 钉连及 install 完整性校验）集中在一个模块，避免各工具各抄一份引发口径漂移。
"""

from __future__ import annotations

import ipaddress
import socket

import httpcore

from .base import ToolError

# NAT64/DNS64、6to4、Teredo：这些 IPv6 段内嵌着 IPv4 地址，经翻译网关可达
# 内网——`is_global` 认它们是公网，但语义上等价于访问内嵌 IPv4。
TRANSLATION_NETS = tuple(
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "2002::/16", "2001::/32")
)


def resolve_public_ips(host: str) -> list[str]:
    """解析主机并校验**全部**结果为公网地址，返回可用 IP 列表（防 SSRF）。"""
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
            if any(ip in net for net in TRANSLATION_NETS):
                raise ToolError(
                    f"web_fetch 拒绝翻译段地址（{host} → {ip}）："
                    "NAT64/6to4/Teredo 可翻译到内网 IPv4"
                )
        text = str(ip)
        if text not in ips:
            ips.append(text)
    if not ips:
        raise ToolError(f"主机 {host} 未解析出任何地址")
    return ips


def install_pinned_backend(transport, backend, label: str) -> None:
    """把自研钉连后端装进 httpx transport，并在私有字段缺失时响亮失败。"""
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError(
            f"{label}：当前 httpx/httpcore 版本的私有结构已变化，"
            "DNS 钉连（防 rebinding）无法安装；拒绝在不设防的情况下发请求"
            "（请按 tools/web.py 的版本锚点注释核对 httpx/httpcore 版本）"
        )
    pool._network_backend = backend


async def connect_pinned_tcp_async(
    inner,
    targets: list[str],
    host: str,
    port: int,
    *,
    timeout: float | None = None,
    local_address: str | None = None,
    socket_options=None,
    connect_errors: tuple[type[BaseException], ...],
    error_cls: type[Exception],
):
    """异步 TCP 连接逐 IP 尝试并固定到已校验列表（PinnedAsyncBackend / _MCPPinnedBackend 共用）。"""
    last: BaseException | None = None
    for ip in targets:
        try:
            return await inner.connect_tcp(
                ip,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        except connect_errors as e:
            last = e
    raise last if last is not None else error_cls(f"没有可用的已校验地址: {host}")


class PinnedAsyncBackend(httpcore.AsyncNetworkBackend):
    """把指定主机的异步 TCP 连接固定到已校验的 IP 列表（防 DNS rebinding）。"""

    def __init__(self, pinned: dict[str, list[str]]) -> None:
        self._pinned = {h.lower(): ips for h, ips in pinned.items()}
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
        return await connect_pinned_tcp_async(
            self._inner,
            targets,
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
            connect_errors=(httpcore.ConnectError, httpcore.ConnectTimeout),
            error_cls=httpcore.ConnectError,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        return await self._inner.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedSyncBackend(httpcore.SyncBackend):
    """把指定主机的同步 TCP 连接固定到已校验的 IP 列表（防 DNS rebinding）。"""

    def __init__(self, pinned: dict[str, list[str]]) -> None:
        self._pinned = {h.lower(): ips for h, ips in pinned.items()}
        self._inner = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        targets = self._pinned.get(host.lower())
        if not targets:
            return self._inner.connect_tcp(
                host, port, timeout=timeout, local_address=local_address,
                socket_options=socket_options,
            )
        last: Exception | None = None
        for ip in targets:
            try:
                return self._inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        raise last if last is not None else httpcore.ConnectError(
            f"没有可用的已校验地址: {host}"
        )

    def connect_unix_socket(self, *args, **kwargs):
        return self._inner.connect_unix_socket(*args, **kwargs)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)
