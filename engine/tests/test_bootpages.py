"""启动页 / 失败页：配色跟随主题深浅（深色系统下不再纸底黑标题栏割裂）。"""

from __future__ import annotations

import base64

from skysheep.bootpages import error_html, splash_html


def test_splash_follows_theme(home):
    light = splash_html(home, dark=False)
    dark = splash_html(home, dark=True)
    assert "background:#e8dfc7" in light
    assert "#171410" not in light  # 浅色版不掺深色底
    assert "background:#171410" in dark
    assert "#f4ecd8" in dark  # 深色版字色提亮


def test_error_page_follows_theme(home):
    light = error_html("boom", "log.txt", dark=False)
    dark = error_html("boom", "log.txt", dark=True)
    assert "#e8dfc7" in light
    assert "#171410" in dark
    assert "boom" in dark and "log.txt" in dark


def test_splash_prefers_white_logo_on_dark(tmp_path):
    """深色版优先白色线稿；浅色版不挑它（白线稿在纸底上不可见）。"""
    (tmp_path / "skysheep-line-white.png").write_bytes(b"fake-png")
    encoded = base64.b64encode(b"fake-png").decode("ascii")
    dark = splash_html(tmp_path, dark=True)
    assert encoded in dark
    light = splash_html(tmp_path, dark=False)
    assert encoded not in light


def test_splash_follows_theme_id(home):
    """主题 id 直传：六个主题各自取色（与 app.css 同名变量同源），未知回退纸墨。

    此前启动页只有浅/深两套，青瓷/秋柿/黛夜/松烟下启动页与界面主题割裂。
    """
    from skysheep import wintheme

    for tid, pal in wintheme.THEME_PALETTE.items():
        page = splash_html(home, theme=tid)
        assert "background:#" + pal["bg"].lower() in page, f"{tid} 启动页底色未跟主题"
    # 未知主题 id 回退纸墨，不抛异常
    assert "background:#e8dfc7" in splash_html(home, theme="nonexistent")


def test_error_page_follows_theme_id(home):
    celadon = error_html("boom", "log.txt", theme="celadon")
    night = error_html("boom", "log.txt", theme="night")
    assert "#dce4da" in celadon and "#e8dfc7" not in celadon
    assert "#171410" in night
    assert "boom" in night and "log.txt" in night
