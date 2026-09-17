"""启动动画页 / 失败页的 HTML 构建。

刻意只依赖标准库：desktop.py 要在**引擎导入之前**就把窗口和动画画出来，
任何沉重的导入（fastapi、引擎核心）都会推迟窗口出现的时间（实测 2.4 秒+）。
"""

from __future__ import annotations

import base64
import html as _html
from pathlib import Path


def splash_html(static_dir: Path) -> str:
    """启动动画页（主窗口的第一页）：纸墨主题 + 小羊 logo 轻浮动 + 三点呼吸。

    内嵌 base64 图片，不依赖服务端口（服务还在后台启动中）。
    logo 优先用云朵小羊 PNG（与官网/应用内一致），旧 SVG 兜底，都没有退回 emoji。
    """
    logo = '<div class="logo-fallback">🐑</div>'
    for name, mime in (("cloud-sheep-icon.png", "image/png"), ("skysheep-logo.svg", "image/svg+xml")):
        logo_path = static_dir / name
        if logo_path.exists():
            b64 = base64.b64encode(logo_path.read_bytes()).decode("ascii")
            logo = f'<img class="logo" alt="" src="data:{mime};base64,{b64}">'
            break
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;height:100%;background:#e8dfc7;overflow:hidden;"
        'font-family:"Microsoft YaHei UI","Microsoft YaHei",sans-serif;user-select:none}'
        ".wrap{height:100%;box-sizing:border-box;margin:14px;display:flex;"
        "flex-direction:column;align-items:center;justify-content:center;gap:18px;"
        "background:#f4ecd8;border:2px solid #1d1a16;border-radius:8px;"
        "box-shadow:3px 3px 0 rgba(29,26,22,.35)}"
        ".logo{width:118px;height:118px;animation:float 1.8s ease-in-out infinite}"
        ".logo-fallback{font-size:84px;line-height:130px;animation:float 1.8s ease-in-out infinite}"
        "@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}"
        ".title{color:#1d1a16;font-size:18px;font-weight:600;letter-spacing:4px}"
        ".sub{color:#6b6255;font-size:13px;letter-spacing:1px}"
        ".dots span{display:inline-block;width:7px;height:7px;border-radius:50%;"
        "background:#1257c4;margin:0 4px;animation:blink 1.2s infinite}"
        ".dots span:nth-child(2){animation-delay:.2s}"
        ".dots span:nth-child(3){animation-delay:.4s}"
        "@keyframes blink{0%,80%,100%{opacity:.15}40%{opacity:1}}"
        "</style></head><body>"
        '<div class="wrap">' + logo + '<div class="title">正在启动 SkySheep</div>'
        '<div class="dots"><span></span><span></span><span></span></div>'
        '<div class="sub">引擎准备中，首次启动可能需要几秒</div>'
        "</div></body></html>"
    )


def error_html(detail: str, log_path: str) -> str:
    """启动失败页：与动画页同一个窗口就地显示，免得用户对着白屏疑惑。"""
    d = _html.escape(detail)
    p = _html.escape(log_path)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;height:100%;background:#e8dfc7;overflow:hidden;"
        'font-family:"Microsoft YaHei UI","Microsoft YaHei",sans-serif}'
        ".wrap{height:100%;box-sizing:border-box;margin:24px;display:flex;"
        "flex-direction:column;gap:14px;background:#f4ecd8;border:2px solid #1d1a16;"
        "border-radius:8px;box-shadow:3px 3px 0 rgba(29,26,22,.35);padding:28px 32px}"
        "h1{margin:0;color:#a03030;font-size:20px;letter-spacing:2px}"
        "pre{margin:0;flex:1;overflow:auto;background:#efe5cb;border:1px solid #cbbfa4;"
        "border-radius:6px;padding:14px;font-size:13px;color:#1d1a16;"
        "font-family:Consolas,monospace;white-space:pre-wrap;word-break:break-all}"
        ".log{color:#6b6255;font-size:13px}"
        "</style></head><body><div class=\"wrap\">"
        "<h1>启动失败</h1>"
        f"<pre>{d}</pre>"
        f'<div class="log">完整信息：{p}</div>'
        "</div></body></html>"
    )
