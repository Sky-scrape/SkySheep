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

import httpx
from pydantic import BaseModel, Field

from ..network import NetworkConfig, make_httpx_client, make_transport
from ._netpin import PinnedSyncBackend as _SyncPinnedBackend
from ._netpin import install_pinned_backend as _install_pinned_backend
from ._netpin import resolve_public_ips as _resolve_public_ips
from .base import ChangeRecorder, Safety, Tool, ToolContext, ToolError, rel_path, resolve_path

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


def _pinned_transport(
    host: str,
    ips: list[str] | None = None,
    network: NetworkConfig | None = None,
) -> httpx.HTTPTransport:
    """返回把连接固定到已校验 IP 的同步 transport。

    ips 不传时先解析校验（必须全部公网）。校验与建连分开做：调用方总是先
    _resolve_public_ips 校验，这里只负责固定——测试注入 transport 接管网络层时
    公网校验也照常发生（M9：校验不能随网络层注入一起被跳过）。
    """
    transport = (
        make_transport(network)
        if network is not None
        else httpx.HTTPTransport(trust_env=False)
    )
    # 与 web_fetch 同一 fail-closed 检查：私有结构变化时响亮失败，
    # 不允许钉连静默退化成「只剩一次 getaddrinfo 校验」（审查 P-17）
    _install_pinned_backend(
        transport, _SyncPinnedBackend({host: ips or _resolve_public_ips(host)}), "generate_image"
    )
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
        network: NetworkConfig | None = None,
    ) -> None:
        self.provider = provider  # zhipu / siliconflow / custom；空 = 未配置
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.recorder = recorder
        self._transport = transport  # 仅测试注入
        self._network = network

    def _sync_client(self, url: str, host: str, ips: list[str]) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(
                timeout=TIMEOUT_S, trust_env=False, transport=self._transport,
                follow_redirects=False,
            )
        pinned = _pinned_transport(host, ips, network=self._network)
        if self._network is not None:
            return make_httpx_client(
                self._network, url=url, transport=pinned, timeout=TIMEOUT_S,
                follow_redirects=False,
            )
        return httpx.Client(
            timeout=TIMEOUT_S, trust_env=False, transport=pinned,
            follow_redirects=False,
        )

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
            await self.recorder.record(p)  # 检查点：改前不存在 → 回滚时删除

        def _write_image_sync() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(image_bytes)

        try:
            await asyncio.to_thread(_write_image_sync)
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
        gen_url = base + "/images/generations"
        gen_host = urlparse(base).hostname or ""
        gen_ips = _resolve_public_ips(gen_host)
        with self._sync_client(gen_url, gen_host, gen_ips) as client:
            with client.stream(
                "POST", gen_url,
                json={"model": model, "prompt": prompt}, headers=headers,
            ) as resp:
                if resp.status_code >= 400:
                    snippet = resp.read()[:200].decode("utf-8", errors="replace")
                    raise ToolError(f"画图接口返回 HTTP {resp.status_code}: {snippet}")
                raw_json = _capped(resp, MAX_API_JSON_BYTES)
                try:
                    data = json.loads(raw_json)
                except ValueError as e:
                    raise ToolError(f"画图接口返回的不是有效 JSON：{e}") from e
            if not isinstance(data, dict):
                raise ToolError("画图接口返回格式无效（非 JSON 对象）")
            items = data.get("data") or data.get("images") or []
            item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
            b64 = item.get("b64_json")
            if b64:
                import binascii

                try:
                    img_bytes = base64.b64decode(b64, validate=True)
                except (ValueError, binascii.Error) as e:
                    raise ToolError(f"画图接口返回的 base64 图片数据无效：{e}") from e
                return "(inline)", img_bytes, "image/png"
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
            with self._sync_client(current, parsed.hostname, dl_ips) as dl_client:
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
