"""临时探针：验证 wintheme.apply_caption_theme 真的把 DWM 属性写上了。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from skysheep.wintheme import hook_caption_theme  # noqa: E402


def readback(window) -> None:
    import ctypes

    native = window.native
    hwnd = int(native.Handle.ToInt64())
    dwm = ctypes.windll.dwmapi
    print(f"hwnd={hwnd:#x}", flush=True)

    def colorref(hex_rgb):
        r, g, b = int(hex_rgb[0:2], 16), int(hex_rgb[2:4], 16), int(hex_rgb[4:6], 16)
        return ctypes.c_uint(r | (g << 8) | (b << 16))

    # 逐属性打印「设置」的 HRESULT（0 = 成功）
    for attr, name, color in (
        (35, "caption-set", colorref("E8DFC7")),
        (36, "text-set", colorref("1D1A16")),
        (34, "border-set", colorref("1D1A16")),
        (20, "immersive-dark(0=浅色)", ctypes.c_int(0)),
    ):
        res = dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(color), 4)
        print(f"{name}: set res={res} ({res & 0xFFFFFFFF:#010x})", flush=True)

    # 再读回（部分系统只支持设置不支持读取）
    for attr, name in ((35, "caption-get"), (36, "text-get")):
        val = ctypes.c_uint(0)
        res = dwm.DwmGetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
        c = val.value
        print(f"{name}: res={res} #{c & 0xFF:02X}{(c >> 8) & 0xFF:02X}{(c >> 16) & 0xFF:02X}", flush=True)


def bootstrap(window) -> None:
    time.sleep(0.8)
    readback(window)
    window.destroy()


w = webview.create_window("DWM probe", html="<p>probe</p>", width=340, height=200)
hook_caption_theme(w)
webview.start(lambda: bootstrap(w))
print("probe done")
