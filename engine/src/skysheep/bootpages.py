"""启动动画页 / 失败页的 HTML 构建。

刻意只依赖标准库：desktop.py 要在**引擎导入之前**就把窗口和动画画出来，
任何沉重的导入（fastapi、引擎核心）都会推迟窗口出现的时间（实测 2.4 秒+）。

配色跟解析后的主题走（浅色 / 深色两套，与 app.css 同族）：启动页、窗口底色、
标题栏从首帧起就是同一套颜色，不再出现「纸色启动页 + 深色标题栏」的割裂。
"""

from __future__ import annotations

import base64
import html as _html
from pathlib import Path

# 两套配色：浅色对标纸墨、深色对标夜墨（改主题色时这里跟 app.css 一起改）
_LIGHT_PAL = {
    "bg": "#e8dfc7", "panel": "#f4ecd8", "line": "#1d1a16",
    "text": "#1d1a16", "sub": "#6b6255", "accent": "#1257c4",
    "shadow": "rgba(29,26,22,.35)",
    "err": "#a03030", "pre_bg": "#efe5cb", "pre_line": "#cbbfa4", "pre_text": "#1d1a16",
}
_DARK_PAL = {
    "bg": "#171410", "panel": "#211d17", "line": "#574c3a",
    "text": "#f4ecd8", "sub": "#9d9179", "accent": "#5b93ee",
    "shadow": "rgba(0,0,0,.55)",
    "err": "#e26255", "pre_bg": "#1b1813", "pre_line": "#3b3428", "pre_text": "#f4ecd8",
}


def _palette(dark: bool) -> dict:
    return _DARK_PAL if dark else _LIGHT_PAL


def splash_html(static_dir: Path, dark: bool = False) -> str:
    """启动动画页（主窗口的第一页）：主题配色 + 小羊 logo 轻浮动 + 三点呼吸。

    内嵌 base64 图片，不依赖服务端口（服务还在后台启动中）。
    logo：浅色主题优先云朵小羊墨色线稿，深色主题优先白色线稿（透明底直显）；
    都没有退回 emoji。
    """
    pal = _palette(dark)
    logo = '<div class="logo-fallback">🐑</div>'
    names = (
        (
            ("skysheep-line-white.png", "image/png"),
            ("cloud-sheep-line.png", "image/png"),
            ("cloud-sheep-icon.png", "image/png"),
            ("skysheep-logo.svg", "image/svg+xml"),
        )
        if dark
        else (
            ("cloud-sheep-line.png", "image/png"),
            ("cloud-sheep-icon.png", "image/png"),
            ("skysheep-logo.svg", "image/svg+xml"),
        )
    )
    for name, mime in names:
        logo_path = static_dir / name
        if logo_path.exists():
            b64 = base64.b64encode(logo_path.read_bytes()).decode("ascii")
            logo = f'<img class="logo" alt="" src="data:{mime};base64,{b64}">'
            break
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;height:100%;background:" + pal["bg"] + ";overflow:hidden;"
        'font-family:"Microsoft YaHei UI","Microsoft YaHei",sans-serif;user-select:none}'
        ".wrap{height:100%;box-sizing:border-box;margin:14px;display:flex;"
        "flex-direction:column;align-items:center;justify-content:center;gap:20px;"
        "background:" + pal["panel"] + ";border:2px solid " + pal["line"] + ";border-radius:8px;"
        "box-shadow:3px 3px 0 " + pal["shadow"] + "}"
        ".brand{display:flex;align-items:center;gap:14px;animation:float 1.8s ease-in-out infinite}"
        ".b-ico{width:52px;height:52px}"
        ".b-ico-fallback{font-size:44px;line-height:52px}"
        ".wordmark{color:" + pal["text"] + ";font-size:34px;font-weight:800;letter-spacing:5px}"
        "@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}"
        ".dots span{display:inline-block;width:7px;height:7px;border-radius:50%;"
        "background:" + pal["accent"] + ";margin:0 4px;animation:blink 1.2s infinite}"
        ".dots span:nth-child(2){animation-delay:.2s}"
        ".dots span:nth-child(3){animation-delay:.4s}"
        "@keyframes blink{0%,80%,100%{opacity:.15}40%{opacity:1}}"
        ".sub{color:" + pal["sub"] + ";font-size:13px;letter-spacing:2px}"
        "</style></head><body>"
        '<div class="wrap">'
        '<div class="brand">' + logo.replace('class="logo"', 'class="b-ico"')
        .replace('class="logo-fallback"', 'class="b-ico-fallback"')
        + '<span class="wordmark">SkySheep</span></div>'
        '<div class="dots"><span></span><span></span><span></span></div>'
        '<div class="sub">正在启动 · 引擎准备中，首次启动可能需要几秒</div>'
        "</div></body></html>"
    )


def error_html(detail: str, log_path: str, dark: bool = False) -> str:
    """启动失败页：与动画页同一个窗口就地显示，免得用户对着白屏疑惑。"""
    pal = _palette(dark)
    d = _html.escape(detail)
    p = _html.escape(log_path)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;height:100%;background:" + pal["bg"] + ";overflow:hidden;"
        'font-family:"Microsoft YaHei UI","Microsoft YaHei",sans-serif}'
        ".wrap{height:100%;box-sizing:border-box;margin:24px;display:flex;"
        "flex-direction:column;gap:14px;background:" + pal["panel"] + ";border:2px solid "
        + pal["line"] + ";border-radius:8px;box-shadow:3px 3px 0 " + pal["shadow"] + ";padding:28px 32px}"
        "h1{margin:0;color:" + pal["err"] + ";font-size:20px;letter-spacing:2px}"
        "pre{margin:0;flex:1;overflow:auto;background:" + pal["pre_bg"] + ";border:1px solid "
        + pal["pre_line"] + ";border-radius:6px;padding:14px;font-size:13px;color:"
        + pal["pre_text"] + ";font-family:Consolas,monospace;white-space:pre-wrap;word-break:break-all}"
        ".log{color:" + pal["sub"] + ";font-size:13px}"
        "</style></head><body><div class=\"wrap\">"
        "<h1>启动失败</h1>"
        f"<pre>{d}</pre>"
        f'<div class="log">完整信息：{p}</div>'
        "</div></body></html>"
    )
