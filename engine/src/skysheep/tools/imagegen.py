"""generate_image 工具：调用图片生成 API 画图，结果保存为项目内图片文件。

支持智谱 CogView、硅基流动 Kolors，以及任意 OpenAI 兼容的
/images/generations 接口；「自动」档按 智谱 → 硅基流动 取第一个配好 Key 的
模型服务，复用已有 Key，零额外配置。

安全模型与 write_file 一致（Safety.WRITE 需确认）：工具会把生成的图片写入
工作目录内的文件，且接入检查点记录器（recorder），回滚时新建的图片一并删除。
下载生成结果与调用生成接口前校验目标地址为公网，并把连接固定到已校验 IP
（防 SSRF 与 DNS rebinding；与 web_fetch 的 _PinnedBackend 同一范式的同步版），
响应体流式限长，超上限立即中止（安全审查 M9）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import urljoin, urlparse

import httpcore
import httpx
from pydantic import BaseModel, Field

from .base import ChangeRecorder, Safety, Tool, ToolContext, ToolError, rel_path, resolve_path
from .web import _resolve_public_ips

TIMEOUT_S = 60.0
MAX_IMAGE_BYTES = 8_000_000
# 生成接口的 JSON 响应上限：b64_json 内联大图时可达数 MB；无上限时恶意/被劫持的
# endpoint 可以让「画一张图」变成内存耗尽（安全审查 M9，与 web_fetch 同思路）
MAX_API_JSON_BYTES = 8_000_000
MAX_REDIRECTS = 4

# 自动档尝试顺序：[(provider 名, 默认模型)]
AUTO_PROVIDERS = (
    ("zhipu", "cogview-3-flash"),
    ("siliconflow", "Kwai-Kolors/Kolors"),
)


class _SyncPinnedBackend(httpcore.SyncBackend):
    """把指定主机的 TCP 连接固定到已校验 IP（web.py _PinnedBackend 的同步镜像）。

    imagegen 在线程里用同步 httpx.Client，复用不了 web_fetch 的 Async 版；
    语义完全一致：命中被固定主机时直连已校验 IP、不再走 DNS（防 rebinding 的
    TOCTOU 窗口），Host 头与 TLS SNI 仍用原主机名（httpcore 生成）。
    """

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


def _pinned_transport(host: str, ips: list[str] | None = None) -> httpx.HTTPTransport:
    """返回把连接固定到已校验 IP 的同步 transport。

    ips 不传时先解析校验（必须全部公网）。校验与建连分开做：调用方总是先
    _resolve_public_ips 校验，这里只负责固定——测试注入 transport 接管网络层时
    公网校验也照常发生（M9：校验不能随网络层注入一起被跳过）。
    """
    transport = httpx.HTTPTransport(trust_env=False)
    transport._pool._network_backend = _SyncPinnedBackend({host: ips or _resolve_public_ips(host)})
    return transport


class GenerateImageArgs(BaseModel):
    prompt: str = Field(description="画面描述（越具体越好：主体、风格、构图、光线）")
    path: str = Field(
        default="",
        description="保存位置（相对工作目录，如 images/poster.png）；缺省自动命名到 images/ 下",
    )


class GenerateImageTool(Tool):
    name = "generate_image"
    description = (
        "AI 画图：按文字描述生成一张图片并保存到项目里（PNG/JPEG）。"
        "用户让你画图、生成插图/海报/封面时使用；生成的是新图片文件，不会改动其他文件。"
    )
    safety = Safety.WRITE
    write_path_arg = True  # 路径缺省时落在工作目录内 images/
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = True
    args_model = GenerateImageArgs

    def __init__(
        self,
        provider: str = "",
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        recorder: ChangeRecorder | None = None,
        transport=None,
    ) -> None:
        self.provider = provider  # zhipu / siliconflow / custom；空 = 未配置
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.recorder = recorder
        self._transport = transport  # 仅测试注入

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.api_key)

    async def run(self, args: GenerateImageArgs, ctx: ToolContext) -> str:
        if not self.configured:
            raise ToolError(
                "图片生成未配置：请到 设置 · 技能与工具 · AI 画图 选择服务商"
                "（智谱 / 硅基流动，或自定义 OpenAI 兼容接口）并确认 API Key 已配置。"
            )
        prompt = (args.prompt or "").strip()
        if not prompt:
            raise ToolError("prompt（画面描述）不能为空")

        url, image_bytes, mime = await asyncio.to_thread(self._generate_sync, prompt)
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ToolError(f"生成的图片太大（{len(image_bytes) / 1048576:.1f}MB），已放弃保存")

        # 落盘：缺省命名 images/img-YYYYmmdd-HHMMSS.png，路径锁在工作目录内
        raw = (args.path or "").strip() or (
            f"images/img-{time.strftime('%Y%m%d-%H%M%S')}"
            + (".jpg" if mime == "image/jpeg" else ".png")
        )
        p = resolve_path(ctx, raw)
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            p = p.with_suffix(".png")
        shown = rel_path(ctx, p)
        if self.recorder is not None:
            self.recorder.record(p)  # 检查点：改前不存在 → 回滚时删除
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(image_bytes)
        except OSError as e:
            raise ToolError(f"cannot save image {shown}: {e}") from e
        return f"image saved: {shown}（{len(image_bytes) / 1024:.0f}KB，来自 {self.provider}）"

    # ---- 请求生成接口并下载结果（同步小函数，线程里跑） ----

    def _generate_sync(self, prompt: str) -> tuple[str, bytes, str]:
        if self.provider == "zhipu":
            base = self.base_url or "https://open.bigmodel.cn/api/paas/v4"
            model = self.model or "cogview-3-flash"
        elif self.provider == "siliconflow":
            base = self.base_url or "https://api.siliconflow.cn/v1"
            model = self.model or "Kwai-Kolors/Kolors"
        elif self.provider == "custom":
            base = self.base_url.rstrip("/")
            if not base:
                raise ToolError("自定义图片服务需要填写接口地址（base_url）")
            model = self.model or "cogview-3-flash"
        else:
            raise ToolError(f"未知的图片服务商: {self.provider}")

        headers = {"Authorization": "Bearer " + self.api_key}

        def _capped(resp: httpx.Response, cap: int) -> bytes:
            """流式读响应体，边读边计数，超上限立即中止（M9：不再先全量进内存）。"""
            buf = bytearray()
            for chunk in resp.iter_bytes(1 << 16):
                if len(buf) + len(chunk) > cap:
                    raise ToolError(f"响应体超过 {cap // (1 << 20)}MB 上限，已中止下载")
                buf += chunk
            return bytes(buf)

        # 生成接口：base_url 是本机设置页（LOCAL_ONLY）配的，默认值是公网官方端点；
        # 但可手填任意值——公网校验无条件做，注入 transport（测试桩）也不例外
        gen_host = urlparse(base).hostname or ""
        gen_ips = _resolve_public_ips(gen_host)
        gen_transport = (
            self._transport if self._transport is not None else _pinned_transport(gen_host, gen_ips)
        )
        with httpx.Client(timeout=TIMEOUT_S, trust_env=False, transport=gen_transport) as client:
            with client.stream(
                "POST", base + "/images/generations",
                json={"model": model, "prompt": prompt}, headers=headers,
            ) as resp:
                if resp.status_code >= 400:
                    snippet = resp.read()[:200].decode("utf-8", errors="replace")
                    raise ToolError(f"画图接口返回 HTTP {resp.status_code}: {snippet}")
                data = json.loads(_capped(resp, MAX_API_JSON_BYTES))
            item = ((data.get("data") or data.get("images")) or [{}])[0] or {}
            b64 = item.get("b64_json")
            if b64:
                return "(inline)", base64.b64decode(b64), "image/png"
            image_url = item.get("url") or ""
            if not image_url:
                raise ToolError(f"画图接口没有返回图片地址：{str(data)[:200]}")

        # 下载生成结果：来源是服务商返回的地址，不受我们控制——每一跳都重新做
        # 公网校验并把连接固定到已校验 IP（防 SSRF 与 DNS rebinding 的 TOCTOU 窗口，
        # M9：与 web_fetch 的 _PinnedBackend 同一范式，同步版）
        current = image_url
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ToolError(f"图片地址协议不支持: {current}")
            dl_ips = _resolve_public_ips(parsed.hostname)  # 内网地址在这里就被拒绝
            dl_transport = (
                self._transport if self._transport is not None
                else _pinned_transport(parsed.hostname, dl_ips)
            )
            with httpx.Client(
                timeout=TIMEOUT_S, trust_env=False, transport=dl_transport,
                follow_redirects=False,
            ) as dl_client:
                with dl_client.stream(
                    "GET", current, headers={"User-Agent": "SkySheep-imagegen/0.7"}
                ) as dl:
                    if dl.status_code in (301, 302, 303, 307, 308):
                        loc = dl.headers.get("location", "")
                        if not loc:
                            raise ToolError("图片下载重定向缺少 Location")
                        # Location 允许是相对路径（RFC 7231），按当前 URL 补全
                        current = urljoin(current, loc)
                        continue
                    if dl.status_code >= 400:
                        raise ToolError(f"图片下载失败 HTTP {dl.status_code}")
                    mime = (dl.headers.get("content-type") or "image/png").split(";")[0].strip()
                    body = _capped(dl, MAX_IMAGE_BYTES)
            return image_url, body, mime
        raise ToolError("图片下载重定向次数过多")
