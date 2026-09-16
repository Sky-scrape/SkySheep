"""把小羊 logo 重 build 成 .NET 兼容的 BMP 帧 ICO。

**背景**：Pillow 生成的 ICO 所有帧都是 PNG 压缩。pywebview 在 Windows 上用 .NET
``System.Drawing.Icon(path)`` 加载窗口/任务栏图标，而 .NET Framework 解析不了
PNG 帧——把 PNG 字节当 BMP 像素硬解，任务栏图标变成花屏渐变块（2026-09-14 用户实测）。

**修法**：从现有 ICO 里解码出原图（PIL 认识 PNG 帧），再手工编码成传统
BMP（BITMAPINFOHEADER + 自底向上 BGRA + AND mask）帧的 ICO，.NET 全兼容。

用法（在 engine 目录）::

    .venv\\Scripts\\python.exe tools\\make_ico.py <源ico或png> <输出ico> [--sizes 16,32,48,64,128,256]
"""

from __future__ import annotations

import argparse
import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

DEFAULT_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _load_rgba(source: Path) -> Image.Image:
    """从 ICO/PNG/SVG 渲染产物中取出最大的帧，转 RGBA。"""
    data = source.read_bytes()
    if data[:4] == b"\x00\x00\x01\x00":  # ICO：挑最大帧解码
        count = struct.unpack_from("<H", data, 4)[0]
        best: tuple[int, Image.Image] | None = None
        for i in range(count):
            w, h, _c, _r, _p, _b, size, off = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
            frame = Image.open(BytesIO(data[off : off + size]))
            frame.load()
            side = w or 256
            if best is None or side > best[0]:
                best = (side, frame)
        if best is None:
            raise ValueError("ICO 里没有帧")
        img = best[1]
    else:
        img = Image.open(BytesIO(data))
    return img.convert("RGBA")


def _bmp_frame(img: Image.Image) -> bytes:
    """把 RGBA 图编码成 ICO 内嵌的 BMP（不含文件头，biHeight=2h，含 AND mask）。"""
    w, h = img.size
    pixels = list(img.getdata())  # RGBA 自顶向下
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        w,
        h * 2,  # XOR + AND 两段
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        0,
        0,
        0,
        0,
        0,
    )
    # 像素：BGRA，自底向上
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = pixels[y * w + x]
            row += struct.pack("<BBBB", b, g, r, a)
        rows.append(bytes(row))
    xor = b"".join(rows)
    # AND mask：1bpp，每行 4 字节对齐；有 alpha 通道时全 0 即可
    stride = ((w + 31) // 32) * 4
    and_mask = b"\x00" * (stride * h)
    return header + xor + and_mask


def build_ico(img: Image.Image, sizes: list[int]) -> bytes:
    base = img
    if base.size != (256, 256):
        base = img.resize((256, 256), Image.LANCZOS)
    frames: list[tuple[int, bytes]] = []
    for side in sorted(set(sizes)):
        frame = base if side == 256 else base.resize((side, side), Image.LANCZOS)
        frames.append((side, _bmp_frame(frame)))

    entries: list[bytes] = []
    blobs: list[bytes] = []
    offset = 6 + 16 * len(frames)
    for side, blob in frames:
        b = side if side < 256 else 0
        entries.append(struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    return struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(blobs)


def main() -> int:
    parser = argparse.ArgumentParser(description="rebuild an ICO with .NET-safe BMP frames")
    parser.add_argument("source", help="源文件：现有 .ico 或 .png")
    parser.add_argument("output", help="输出 .ico 路径")
    parser.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    args = parser.parse_args()

    src, dst = Path(args.source), Path(args.output)
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if not sizes:
        print("[错误] --sizes 为空")
        return 2

    img = _load_rgba(src)
    ico = build_ico(img, sizes)
    dst.write_bytes(ico)
    print(f"[完成] {dst}  {len(ico)/1024:.0f} KB  帧尺寸={sorted(set(sizes))}（BMP 帧）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
