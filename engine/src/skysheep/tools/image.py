"""read_image 工具：把本地图片附加到对话里，让模型直接看见它。

此前只有 screenshot 能把图像送进上下文，用户拿 `@图片.png` 或让 Agent 看一张
截图时只能得到一堆替换字符（read_file 按 utf-8 硬解二进制）。这里补齐同一个
能力：读本地图片 → 压到模型友好的尺寸 → 作为 ImageBlock 附加，机制与 screenshot
完全一致（agent 循环把 ctx.images 转成 user 消息）。

与 screenshot 的区别只在来源：screenshot 抓屏幕，read_image 读磁盘文件。
因此两者共用同一套「模型不支持图片输入时给出可读提示」的前置检查。
"""

from __future__ import annotations

import asyncio
import base64
import io
import struct

from pydantic import BaseModel, Field

from ..messages import ImageBlock
from .base import Safety, Tool, ToolContext, ToolError, rel_path, resolve_path

# 与 screenshot 一致的上限：宽度超过就等比缩小，视频模型的视觉 token 与
# 请求体大小都随之可控。
MAX_IMAGE_WIDTH = 1568
MAX_IMAGE_BYTES = 20_000_000  # 单文件上限：再大基本是拿错了（或该压缩后再说）

SUPPORTED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# 送进模型的媒体类型（图片输入的四种；后缀 → MIME）
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ReadImageArgs(BaseModel):
    path: str = Field(description="图片文件路径（相对当前工作目录或绝对路径）")


def _sniff_media_type(data: bytes, suffix: str) -> str | None:
    """按文件头判断真实类型，避免把 .png 后缀的非图片当图片送去 API。

    只用魔数、不引依赖：这是「是不是图片」这种廉价判断，不值得为它装一个库。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _shrink(data: bytes, media_type: str) -> tuple[bytes, bool]:
    """超过宽度上限时等比缩小并重编码；Pillow 不可用或解码失败时原样返回。"""
    try:
        from PIL import Image  # noqa: PLC0415 - 可选依赖，缺失时退化为原图
    except ImportError:
        return data, False
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width <= MAX_IMAGE_WIDTH:
                return data, False
            scale = MAX_IMAGE_WIDTH / img.width
            resized = img.convert("RGB").resize(
                (MAX_IMAGE_WIDTH, max(1, round(img.height * scale)))
            )
            buf = io.BytesIO()
            resized.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), True
    except (OSError, ValueError, struct.error):
        return data, False


def _prepare_image(data: bytes) -> tuple[bytes, str, bool]:
    """缩放（需要时）+ base64 编码，供 to_thread 调用。返回 (最终字节, base64, 是否缩放)。"""
    shrunk_data, shrunk = _shrink(data, "")
    return shrunk_data, base64.b64encode(shrunk_data).decode(), shrunk


class ReadImageTool(Tool):
    name = "read_image"
    description = (
        "读取本地图片文件并把图像附加到对话中（PNG / JPG / WebP / GIF），"
        "你能直接看到画面内容。用户提到截图、照片、设计稿、图表图片时用它；"
        "要看当前屏幕请用 screenshot。图片路径不能是目录。"
    )
    safety = Safety.READONLY
    args_model = ReadImageArgs

    async def run(self, args: ReadImageArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        if not p.exists():
            raise ToolError("file not found: " + shown)
        if p.is_dir():
            raise ToolError(f"{shown} 是目录，read_image 只能读图片文件")
        suffix = p.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ToolError(
                f"{shown} 不是支持的图片格式（{' / '.join(SUPPORTED_SUFFIXES)}）。"
                "PDF/Word/Excel/PPT 用 read_document，其它二进制用 run_command 处理。"
            )
        if not getattr(ctx, "supports_vision", True):
            raise ToolError(
                "当前模型不支持图片输入，读进来的图它看不到。"
                "请在输入框的模型选择器里换一个多模态模型（如 GLM-4V、Kimi、GPT-4o 等），"
                "或改用 read_document / read_file 走纯文本方式。"
            )
        try:
            size = p.stat().st_size
        except OSError as e:
            raise ToolError("cannot stat " + shown + ": " + str(e)) from e
        if size > MAX_IMAGE_BYTES:
            raise ToolError(
                f"图片过大（{size / 1048576:.1f}MB，上限 {MAX_IMAGE_BYTES // 1048576}MB）：{shown}"
            )
        try:
            data = p.read_bytes()
        except OSError as e:
            raise ToolError("cannot read " + shown + ": " + str(e)) from e
        media_type = _sniff_media_type(data, suffix)
        if media_type is None:
            raise ToolError(
                f"{shown} 的内容不是有效图片（后缀是 {suffix}，但缺少对应文件头）。"
                "文件可能损坏，或改名改错了。"
            )
        # PIL 解码/缩放/optimize 编码与大文件 base64 都是毫秒到秒级的 CPU 活，
        # 与 screenshot 一样放线程做，不卡事件循环
        data, b64, shrunk = await asyncio.to_thread(_prepare_image, data)
        ctx.images.append(ImageBlock(media_type=media_type, data=b64))
        lines = [
            f"图片已附加到本条消息之后（模型可直接查看）：{shown}",
            f"- 格式: {media_type}，原始大小: {size:,} B",
        ]
        if shrunk:
            lines.append(
                f"- 已等比缩小到宽度 {MAX_IMAGE_WIDTH}px 后重编码为 PNG（视觉细节可能损失，"
                "需要看清小字时请让用户截取局部）"
            )
        return "\n".join(lines)
