"""把小羊 logo 重 build 成 .NET 兼容的 BMP 帧 ICO。

**背景**：Pillow 生成的 ICO 所有帧都是 PNG 压缩。pywebview 在 Windows 上用 .NET
``System.Drawing.Icon(path)`` 加载窗口/任务栏图标，而 .NET Framework 解析不了
PNG 帧——把 PNG 字节当 BMP 像素硬解，任务栏图标变成花屏渐变块（2026-09-14 用户实测）。
**修法**：手工编码成传统 BMP（BITMAPINFOHEADER + 自底向上 BGRA + AND mask）帧的 ICO，
.NET 全兼容。

**小尺寸不再缩原图（2026-11-04）**：原先每一帧都是 256px 原图 LANCZOS 缩出来的。原图
自带抗锯齿，再缩一次等于二次采样——32px 上 12.5px 的描边只剩 1.5px 灰线，羊毛齿的锯齿
被缩成抖动，200% DPI 下托盘/任务栏看是一团糊（用户实测截图）。``--art`` 模式改成
**描轮廓 + 按尺寸重新光栅化**：从 256 帧描出羊毛环的外/内边界与两只眼睛（几何一个点
不改，齿数、齿形、眼睛大小、方块都在原位），再在目标尺寸上按 16× 超采样画一遍后
BOX 降采样。效果是同一只羊、同样的细线，但边缘只有一次抗锯齿，不再糊。

用法（在 engine 目录）::

    # 三套一起重画（彩色方块 / 墨线 / 白线），几何取自现网 skysheep.ico 的 256 帧
    .venv\\Scripts\\python.exe tools\\make_ico.py --art all --out-dir src/skysheep/server/static

    # 只重画一套（--master 指定描形用的源 ico，缺省用输出旁的 skysheep.ico）
    .venv\\Scripts\\python.exe tools\\make_ico.py --art color --out src/skysheep/server/static/skysheep.ico

    # 旧路径：把现成 ICO/PNG 的帧重新编码成 BMP 帧
    .venv\\Scripts\\python.exe tools\\make_ico.py <源ico或png> <输出ico> [--sizes 16,32,48,64,128,256]
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

DEFAULT_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

INK = (26, 26, 26)
WHITE = (255, 255, 255)
PLATE_FILL = (227, 240, 254)

# 方块底几何（相对画布，从 256 原图量出来）：内缩 5%、圆角 0.18、内白边 1.2%
PLATE_INSET = 0.05
PLATE_RADIUS = 0.18
PLATE_RIM = 0.012

# 描边的下限：等比缩放时 16px 的线只有 0.8px，压到 1px 以免直接消失（不改形状只补线宽）
MIN_STROKE_PX = 1.0

# 轮廓采样点数（按角度等分）；平滑只抹亚像素采样残留，远小于 12 齿的 30° 周期
TRACE_SAMPLES = 720
TRACE_SMOOTH = 0.7

# 覆盖率归一用的两端亮度：盘子底≈240、墨线≈26
BG_LUM = 240.0
INK_LUM = 26.0
# 边界取 0.5 覆盖率处（像素面积意义的中线）
EDGE_LEVEL = 0.5

ART_FILES = {
    "color": ("skysheep.ico", True, INK, PLATE_FILL),
    "ink": ("skysheep-line-ink.ico", False, INK, PLATE_FILL),
    "white": ("skysheep-line-white.ico", False, WHITE, PLATE_FILL),
}


def _load_rgba(source: Path) -> Image.Image:
    """打开源图并转 RGBA；ICO 取最大的一帧（逐帧字节切片 PIL 解不了 BMP 帧，得让它自己挑）。"""
    img = Image.open(source)
    if getattr(img, "format", "") == "ICO":
        sizes = img.info.get("sizes") or {img.size}
        side = max(s[0] for s in sizes)
        img.size = (side, side)
        img.load()
    return img.convert("RGBA")


def _bmp_frame(img: Image.Image) -> bytes:
    """把 RGBA 图编码成 ICO 内嵌的 BMP（不含文件头，biHeight=2h，含 AND mask）。"""
    w, h = img.size
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
    # 像素：BGRA，自底向上（PIL 直接按 BGRA 拆字节，比逐像素 struct 快得多）
    raw = img.tobytes("raw", "BGRA")
    stride = w * 4
    xor = b"".join(raw[y * stride : (y + 1) * stride] for y in range(h - 1, -1, -1))
    # AND mask：1bpp，每行 4 字节对齐；有 alpha 通道时全 0 即可
    mask_stride = ((w + 31) // 32) * 4
    and_mask = b"\x00" * (mask_stride * h)
    return header + xor + and_mask


def _pack_ico(frames: list[tuple[int, Image.Image]]) -> bytes:
    """把若干 (边长, 图) 打包成 ICO 字节流（BMP 帧，.NET 可读）。"""
    entries: list[bytes] = []
    blobs: list[bytes] = []
    offset = 6 + 16 * len(frames)
    for side, img in frames:
        blob = _bmp_frame(img)
        b = side if side < 256 else 0
        entries.append(struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    return struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(blobs)


def build_ico(img: Image.Image, sizes: list[int]) -> bytes:
    """旧路径：一整张图按尺寸 LANCZOS 缩放后打包（会二次采样，小尺寸偏糊）。"""
    base = img
    if base.size != (256, 256):
        base = img.resize((256, 256), Image.LANCZOS)
    frames = [
        (side, base if side == 256 else base.resize((side, side), Image.LANCZOS))
        for side in sorted(set(sizes))
    ]
    return _pack_ico(frames)


# ---- 描形：从 256 帧取出羊毛环与眼睛的几何，一个点都不改 ----


def _ink_pixels(img: Image.Image) -> list[tuple[int, int]]:
    """深色不透明像素（环 + 眼睛）。"""
    px = img.load()
    out: list[tuple[int, int]] = []
    for y in range(img.size[1]):
        for x in range(img.size[0]):
            r, g, b, a = px[x, y]
            if a > 128 and (0.299 * r + 0.587 * g + 0.114 * b) < 120:
                out.append((x, y))
    return out


def _components(points: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """4 连通分组（环是一大块，两只眼睛是各自的孤立小块）。"""
    cells = set(points)
    comps: list[list[tuple[int, int]]] = []
    while cells:
        start = cells.pop()
        comp = [start]
        stack = [start]
        while stack:
            x, y = stack.pop()
            for nxt in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if nxt in cells:
                    cells.remove(nxt)
                    stack.append(nxt)
                    comp.append(nxt)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


def _smooth_closed(values: list[float], sigma: float) -> list[float]:
    """环形一维平滑：只抹掉亚像素采样残留的毛刺，不碰 12 齿的轮廓。"""
    if sigma <= 0:
        return values
    n = len(values)
    half = max(1, int(round(sigma * 3)))
    weights = [math.exp(-0.5 * ((i - half) / sigma) ** 2) for i in range(half * 2 + 1)]
    total = sum(weights)
    weights = [w / total for w in weights]
    return [sum(weights[j] * values[(i + j - half) % n] for j in range(half * 2 + 1)) for i in range(n)]


def _coverage(img: Image.Image, points: list[tuple[int, int]]) -> dict[tuple[int, int], float]:
    """某个连通块内每个像素的墨迹覆盖率（0~1）：深色与 alpha 归一成一个量。"""
    px = img.load()
    span = BG_LUM - INK_LUM
    out: dict[tuple[int, int], float] = {}
    for x, y in points:
        r, g, b, a = px[x, y]
        if a == 0:
            continue
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        value = max(0.0, min(1.0, (BG_LUM - lum) / span)) * (a / 255.0)
        if value > 0:
            out[(x, y)] = value
    return out


def _sample(cov: dict[tuple[int, int], float], x: float, y: float) -> float:
    """双线性采样；块外一律 0（只描这一块，别让射线蹭到旁边的眼睛/环）。"""
    x0, y0 = int(x), int(y)
    fx, fy = x - x0, y - y0
    c00 = cov.get((x0, y0), 0.0)
    c10 = cov.get((x0 + 1, y0), 0.0)
    c01 = cov.get((x0, y0 + 1), 0.0)
    c11 = cov.get((x0 + 1, y0 + 1), 0.0)
    return c00 * (1 - fx) * (1 - fy) + c10 * fx * (1 - fy) + c01 * (1 - fx) * fy + c11 * fx * fy


def _edge_radius(
    cov: dict[tuple[int, int], float],
    cx: float,
    cy: float,
    angle: float,
    r_from: float,
    r_to: float,
    step: float = 0.5,
) -> float | None:
    """沿射线找 0.5 覆盖率的亚像素半径（线性插值），找不到返回 None。"""
    ca, sa = math.cos(angle), math.sin(angle)
    count = int(round((r_to - r_from) / step))
    prev_r = r_from
    prev_c = _sample(cov, cx + prev_r * ca, cy + prev_r * sa)
    for i in range(1, count + 1):
        r = r_from + step * i
        c = _sample(cov, cx + r * ca, cy + r * sa)
        if (prev_c - EDGE_LEVEL) * (c - EDGE_LEVEL) <= 0 and abs(c - prev_c) > 1e-6:
            t = (EDGE_LEVEL - prev_c) / (c - prev_c)
            return prev_r + step * t
        prev_r, prev_c = r, c
    return None


def _profile(
    cov: dict[tuple[int, int], float], cx: float, cy: float, r_from: float, r_to: float, step: float
) -> list[float]:
    """整圈采样出半径序列；个别射线没打到时用邻值补，再做极轻的平滑。"""
    radii: list[float | None] = []
    for i in range(TRACE_SAMPLES):
        angle = 2 * math.pi * i / TRACE_SAMPLES
        radii.append(_edge_radius(cov, cx, cy, angle, r_from, r_to, step))
    known = [i for i, r in enumerate(radii) if r is not None]
    if not known:
        raise ValueError("射线没打到轮廓（描形源不对？）")
    filled: list[float] = []
    for i in range(TRACE_SAMPLES):
        value = radii[i]
        if value is not None:
            filled.append(value)
            continue
        prev = max((k for k in known if k < i), default=known[-1] - TRACE_SAMPLES)
        nxt = min((k for k in known if k > i), default=known[0] + TRACE_SAMPLES)
        a = radii[prev % TRACE_SAMPLES]
        b = radii[nxt % TRACE_SAMPLES]
        assert a is not None and b is not None
        t = (i - prev) / (nxt - prev) if nxt != prev else 0.0
        filled.append(a + (b - a) * t)
    return _smooth_closed(filled, TRACE_SMOOTH)


def _coverage_all(img: Image.Image) -> dict[tuple[int, int], float]:
    """整张图的墨迹覆盖率（含软边），用来称墨量。"""
    return _coverage(img, [(x, y) for y in range(img.size[1]) for x in range(img.size[0])])


def _mass_width(master: Image.Image, cx: float, cy: float, outer: list[float], inner: list[float]) -> float:
    """墨量/周长换算的"视觉线宽"。

    原图的边缘是软的（0→满覆盖率要 8px/256 画布），墨量摊在软边上；只描 50% 线会明显
    比现在细。这里把环+眼睛一整块的墨量（方块底那圈投影不算）除以中线周长，得到它实际
    压出来的线宽，差额在描形后补回去。
    """
    perimeter = 0.0
    mid = [(o + i) / 2 for o, i in zip(outer, inner, strict=True)]
    for k in range(TRACE_SAMPLES):
        a0 = 2 * math.pi * k / TRACE_SAMPLES
        a1 = 2 * math.pi * (k + 1) / TRACE_SAMPLES
        x0, y0 = mid[k] * math.cos(a0), mid[k] * math.sin(a0)
        x1, y1 = mid[(k + 1) % TRACE_SAMPLES] * math.cos(a1), mid[(k + 1) % TRACE_SAMPLES] * math.sin(a1)
        perimeter += math.hypot(x1 - x0, y1 - y0)
    if not perimeter:
        return 0.0
    cut = max(outer) + 10  # 环内的墨（含眼睛、含软边），圈外是方块底的白边/投影
    mass = 0.0
    for (px, py), value in _coverage_all(master).items():
        if math.hypot(px + 0.5 - cx, py + 0.5 - cy) <= cut:
            mass += value
    return mass / perimeter


def trace_art(master: Image.Image) -> dict:
    """把 256 帧描成几何：中心、环的外/内半径序列、每只眼睛的轮廓。

    坐标一律是源画布下的像素值，渲染时整体乘 size/side；形状不做任何简化。
    外缘从外往里扫、内缘从圆心往外扫，各自只在所属连通块里找边界——
    这样射线不会先撞到眼睛，也不会在齿谷把自己扫穿。
    """
    side = master.size[0]
    comps = _components(_ink_pixels(master))
    if not comps:
        raise ValueError("源图里没有深色像素")
    ring = comps[0]
    eyes = [c for c in comps[1:] if len(c) >= 20]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    cx = (min(xs) + max(xs) + 1) / 2
    cy = (min(ys) + max(ys) + 1) / 2
    r_far = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2 + 6
    ring_cov = _coverage(master, ring)
    outer = _profile(ring_cov, cx, cy, r_far, 0.0, -0.5)
    inner = _profile(ring_cov, cx, cy, 0.0, r_far, 0.5)
    traced_eyes = []
    for comp in eyes:
        exs = [p[0] for p in comp]
        eys = [p[1] for p in comp]
        ex = (min(exs) + max(exs) + 1) / 2
        ey = (min(eys) + max(eys) + 1) / 2
        reach = max(math.hypot(x + 0.5 - ex, y + 0.5 - ey) for x, y in comp) + 2
        traced_eyes.append(
            {
                "center": (ex, ey),
                "radii": _profile(_coverage(master, comp), ex, ey, 0.0, reach, 0.5),
            }
        )
    # 按墨量把软边摊掉的线宽补回去（形状不变，视觉重量照旧）
    width = _mean_width({"outer": outer, "inner": inner})
    grow = max(0.0, (_mass_width(master, cx, cy, outer, inner) - width) / 2)
    return {
        "side": side,
        "center": (cx, cy),
        "outer": [r + grow for r in outer],
        "inner": [r - grow for r in inner],
        "eyes": [
            {"center": e["center"], "radii": [r + grow for r in e["radii"]]} for e in traced_eyes
        ],
    }


# ---- 按尺寸重新光栅化 ----


def _ring_points(radii: list[float], cx: float, cy: float, scale: float) -> list[tuple[float, float]]:
    pts = []
    for i, r in enumerate(radii):
        a = 2 * math.pi * i / TRACE_SAMPLES
        pts.append(((cx + r * math.cos(a)) * scale, (cy + r * math.sin(a)) * scale))
    return pts


def render_frame(
    size: int, *, plate: bool, ink: tuple[int, int, int], plate_fill: tuple[int, int, int], art: dict
) -> Image.Image:
    """按目标尺寸重画一帧：方块底 + 描出来的羊毛环 + 两只眼，超采样后 BOX 降采样。"""
    ss = 16 if size <= 64 else 8
    canvas = size * ss
    scale = canvas / art["side"]
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if plate:
        inset, radius = PLATE_INSET * canvas, PLATE_RADIUS * canvas
        box = (inset, inset, canvas - inset, canvas - inset)
        draw.rounded_rectangle(box, radius=radius, fill=(*plate_fill, 255))
        rim = PLATE_RIM * canvas
        if rim >= 0.6:  # 太细的白边到小尺寸只会变成一圈灰，直接不画
            draw.rounded_rectangle(box, radius=radius, outline=(255, 255, 255, 255), width=max(1, round(rim)))

    cx, cy = art["center"]
    # 线宽下限（只在 16/20px 生效）：等比缩放后不足 1px 时沿半径补一点，形状不动
    grow = max(0.0, (MIN_STROKE_PX * art["side"] / size - _mean_width(art)) / 2)
    inner_fill = (*plate_fill, 255) if plate else (0, 0, 0, 0)
    draw.polygon(_ring_points([r + grow for r in art["outer"]], cx, cy, scale), fill=(*ink, 255))
    draw.polygon(_ring_points([r - grow for r in art["inner"]], cx, cy, scale), fill=inner_fill)

    for eye in art["eyes"]:
        eye_cx, eye_cy = eye["center"]
        draw.polygon(
            _ring_points([r + grow for r in eye["radii"]], eye_cx, eye_cy, scale), fill=(*ink, 255)
        )
    return img.resize((size, size), Image.BOX)


def _mean_width(art: dict) -> float:
    diffs = [o - i for o, i in zip(art["outer"], art["inner"], strict=True)]
    return sum(diffs) / len(diffs)


def build_art_ico(
    size_list: list[int],
    *,
    plate: bool,
    ink: tuple[int, int, int],
    plate_fill: tuple[int, int, int],
    art: dict,
) -> bytes:
    frames = [
        (size, render_frame(size, plate=plate, ink=ink, plate_fill=plate_fill, art=art)) for size in size_list
    ]
    return _pack_ico(frames)


def _parse_sizes(text: str) -> list[int]:
    return sorted({int(s) for s in text.split(",") if s.strip()})


def main() -> int:
    parser = argparse.ArgumentParser(description="rebuild an ICO with .NET-safe BMP frames")
    parser.add_argument("source", nargs="?", help="源文件：现有 .ico 或 .png（旧路径）")
    parser.add_argument("output", nargs="?", help="输出 .ico 路径（旧路径 / --art 单套输出）")
    parser.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    parser.add_argument(
        "--art", choices=("color", "ink", "white", "all"), help="描轮廓后按尺寸重画（几何照旧，不缩原图）"
    )
    parser.add_argument("--out-dir", help="--art all 的输出目录（应用 static 目录）")
    parser.add_argument("--out", help="--art 单套的输出路径")
    parser.add_argument("--master", help="描形用的源 ico（缺省：输出目录里的 skysheep.ico）")
    args = parser.parse_args()

    sizes = _parse_sizes(args.sizes)
    if not sizes:
        print("[错误] --sizes 为空")
        return 2

    if args.art:
        if args.art == "all":
            if not args.out_dir:
                print("[错误] --art all 需要 --out-dir")
                return 2
            out_dir = Path(args.out_dir)
            targets = [
                (out_dir / ART_FILES[name][0], *ART_FILES[name][1:]) for name in ("color", "ink", "white")
            ]
        else:
            name, plate, ink, plate_fill = ART_FILES[args.art]
            out = args.out or args.output
            if not out:
                print("[错误] --art 需要 --out（或给第二个位置参数）")
                return 2
            targets = [(Path(out), plate, ink, plate_fill)]
        master_path = Path(args.master) if args.master else targets[0][0].with_name("skysheep.ico")
        if not master_path.exists():
            print(f"[错误] 描形源 {master_path} 不存在；用 --master 指定一个 256 帧的 skysheep.ico")
            return 2
        art = trace_art(_load_rgba(master_path))
        print(
            f"[描形] {master_path}  环宽 {_mean_width(art):.1f}px(256 画布)  眼睛 {len(art['eyes'])} 只"
        )
        for path, plate, ink, plate_fill in targets:
            ico = build_art_ico(sizes, plate=plate, ink=ink, plate_fill=plate_fill, art=art)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(ico)
            print(f"[完成] {path}  {len(ico)/1024:.0f} KB  帧尺寸={sizes}（按尺寸重新光栅化）")
        return 0

    if not args.source or not args.output:
        parser.error("旧路径需要 <源ico或png> <输出ico>，或改用 --art")
    src, dst = Path(args.source), Path(args.output)
    img = _load_rgba(src)
    ico = build_ico(img, sizes)
    dst.write_bytes(ico)
    print(f"[完成] {dst}  {len(ico)/1024:.0f} KB  帧尺寸={sorted(set(sizes))}（BMP 帧）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
