"""generate_image 工具：调用图片生成 API 画图，结果保存为项目内图片文件。

支持智谱 CogView、硅基流动 Kolors，以及任意 OpenAI 兼容的
/images/generations 接口；「自动」档按 智谱 → 硅基流动 取第一个配好 Key 的
模型服务，复用已有 Key，零额外配置。

安全模型与 write_file 一致（Safety.WRITE 需确认）：工具会把生成的图片写入
工作目录内的文件，且接入检查点记录器（recorder），回滚时新建的图片一并删除。
下载生成结果前校验目标地址为公网（复用 web_fetch 的 SSRF 防护）。
"""

from __future__ import annotations

import asyncio
import base64
import time

import httpx
from pydantic import BaseModel, Field

from .base import ChangeRecorder, Safety, Tool, ToolContext, ToolError, rel_path, resolve_path
from .web import _assert_public_host

TIMEOUT_S = 60.0
MAX_IMAGE_BYTES = 8_000_000

# 自动档尝试顺序：[(provider 名, 默认模型)]
AUTO_PROVIDERS = (
    ("zhipu", "cogview-3-flash"),
    ("siliconflow", "Kwai-Kolors/Kolors"),
)


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
    last_diff = ""

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
        with httpx.Client(timeout=TIMEOUT_S, trust_env=False, transport=self._transport) as client:
            resp = client.post(
                base + "/images/generations",
                json={"model": model, "prompt": prompt},
                headers=headers,
            )
            if resp.status_code >= 400:
                raise ToolError(f"画图接口返回 HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            item = ((data.get("data") or data.get("images")) or [{}])[0] or {}
            b64 = item.get("b64_json")
            if b64:
                return "(inline)", base64.b64decode(b64), "image/png"
            image_url = item.get("url") or ""
            if not image_url:
                raise ToolError(f"画图接口没有返回图片地址：{str(data)[:200]}")
            # 下载生成结果：来源是服务商返回的地址，逐跳做公网校验（防 SSRF）
            from urllib.parse import urlparse

            current = image_url
            for _ in range(4):
                parsed = urlparse(current)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ToolError(f"图片地址协议不支持: {current}")
                _assert_public_host(parsed.hostname)
                dl = client.get(current, headers={"User-Agent": "SkySheep-imagegen/0.7"})
                if dl.status_code in (301, 302, 303, 307, 308):
                    loc = dl.headers.get("location", "")
                    if not loc:
                        raise ToolError("图片下载重定向缺少 Location")
                    current = loc
                    continue
                if dl.status_code >= 400:
                    raise ToolError(f"图片下载失败 HTTP {dl.status_code}")
                mime = (dl.headers.get("content-type") or "image/png").split(";")[0].strip()
                return image_url, dl.content, mime
            raise ToolError("图片下载重定向次数过多")
