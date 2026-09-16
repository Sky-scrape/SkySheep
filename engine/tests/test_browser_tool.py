"""浏览器控制工具测试：URL 校验 / 搜索拼串 / 打开动作 / 白名单粒度。

不真开浏览器：_open_in_browser 被 monkeypatch 掉，只验证参数链路与安全边界。
"""

from __future__ import annotations

import pytest

from skysheep.tools.base import ToolContext, ToolError
from skysheep.tools.browser import (
    BrowserTool,
    build_search_url,
    validate_url,
)


def _ctx(tmp_path):
    return ToolContext(working_dir=tmp_path)


# ---- URL 校验（只放行 http/https） ----


def test_validate_url_accepts_http_https():
    assert validate_url("https://example.com") == "https://example.com"
    assert validate_url("http://192.168.1.5:8080/app") == "http://192.168.1.5:8080/app"
    assert validate_url("  https://a.b/c?q=1  ") == "https://a.b/c?q=1"


@pytest.mark.parametrize(
    "bad",
    ["", "file:///C:/Windows", "javascript:alert(1)", "ftp://x.com", "/relative/path"],
)
def test_validate_url_rejects_non_http(bad):
    with pytest.raises(ToolError):
        validate_url(bad)


# ---- 搜索拼串 ----


def test_build_search_url_engines():
    assert build_search_url("bing", "SkySheep") == "https://www.bing.com/search?q=SkySheep"
    assert "wd=%E4%BD%A0%E5%A5%BD" in build_search_url("baidu", "你好")
    assert build_search_url("duckduckgo", "a b").startswith("https://duckduckgo.com/?q=a%20")


def test_build_search_url_unknown_engine():
    with pytest.raises(ToolError):
        build_search_url("google", "x")


# ---- 工具执行（open/search 均不真开浏览器） ----


async def test_browser_open_uses_system_browser(tmp_path, monkeypatch):
    opened = []
    import skysheep.tools.browser as mod

    monkeypatch.setattr(mod, "_open_in_browser", lambda url: opened.append(url))
    out = await BrowserTool().run(
        BrowserTool.args_model(action="open", url="https://example.com"),
        _ctx(tmp_path),
    )
    assert opened == ["https://example.com"]
    assert "example.com" in out


async def test_browser_open_rejects_bad_url_without_opening(tmp_path, monkeypatch):
    opened = []
    import skysheep.tools.browser as mod

    monkeypatch.setattr(mod, "_open_in_browser", lambda url: opened.append(url))
    with pytest.raises(ToolError):
        await BrowserTool().run(
            BrowserTool.args_model(action="open", url="file:///C:/x"), _ctx(tmp_path)
        )
    assert opened == []  # 拒绝路径绝不碰浏览器


async def test_browser_search_builds_url_and_mentions_limitation(tmp_path, monkeypatch):
    opened = []
    import skysheep.tools.browser as mod

    monkeypatch.setattr(mod, "_open_in_browser", lambda url: opened.append(url))
    tool = BrowserTool()
    out = await tool.run(
        tool.args_model(action="search", query="SkySheep 发布", engine="bing"), _ctx(tmp_path)
    )
    assert opened == ["https://www.bing.com/search?q=SkySheep%20%E5%8F%91%E5%B8%83"]
    assert "web_fetch" in out  # 提示模型自己读内容要走 web_fetch


async def test_browser_search_requires_query(tmp_path, monkeypatch):
    import skysheep.tools.browser as mod

    monkeypatch.setattr(mod, "_open_in_browser", lambda url: None)
    with pytest.raises(ToolError):
        await BrowserTool().run(
            BrowserTool.args_model(action="search", query="  "), _ctx(tmp_path)
        )


# ---- 安全元数据与白名单粒度 ----


def test_browser_tool_is_dangerous_and_action_prefixed():
    """高危确认制；「总是允许」按动作前缀生成（arg_text 首词 = action）。"""
    tool = BrowserTool()
    assert tool.safety.value == "dangerous"
    assert tool.arg_text({"action": "open", "url": "https://a.b/c"}).startswith("open ")
    assert tool.arg_text({"action": "search", "query": "x"}).startswith("search ")
