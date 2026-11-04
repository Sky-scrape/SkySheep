"""I 组 · 真实浏览器验收（Playwright 可选层）。

依赖策略与 tests/test_browser_ui.py 完全一致：playwright 在独立的 browser
依赖组，未安装时整组 skip（pytest.importorskip），不拖累日常套件。

    uv sync --group browser && uv run python -m playwright install chromium
    uv run pytest tests/acceptance/test_ui_journey.py
"""

from __future__ import annotations

import contextlib

import pytest

pytest.importorskip("playwright.async_api", reason="未安装 playwright（可选依赖）")

from _helpers import live_server  # noqa: E402
from playwright.async_api import async_playwright, expect  # noqa: E402

from skysheep.messages import TextBlock, ToolUseBlock  # noqa: E402

pytestmark = [pytest.mark.acceptance, pytest.mark.e2e, pytest.mark.slow]


@contextlib.asynccontextmanager
async def browser_page(base_url: str, **viewport):
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - 二进制没下载就跳过
            raise pytest.skip.Exception(
                f"Chromium 不可用（先跑 playwright install chromium）：{e}"
            ) from e
        page = await browser.new_page(viewport=viewport or {"width": 1280, "height": 800})
        await page.goto(base_url, wait_until="domcontentloaded")
        try:
            yield page
        finally:
            await browser.close()


async def _open_main_view(page):
    done = page.locator("#wizard-done, #btn-wizard-done")
    if await done.count():
        with contextlib.suppress(Exception):
            await done.first.click(timeout=2000)


async def test_i1_skeleton_and_enter_send(home):
    """I1 界面骨架可加载；Enter 发送、Shift+Enter 换行。"""
    async with live_server(home, [[TextBlock(text="回答")]]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            assert await page.locator("#btn-send").count() == 1

            await page.click("#input")
            await page.type("#input", "第一行")
            await page.keyboard.press("Shift+Enter")
            await page.type("#input", "第二行")
            assert "\n" in await page.input_value("#input")

            await page.keyboard.press("Enter")
            await expect(page.locator("#input")).to_have_value("", timeout=10000)


async def test_i2_permission_card_allow_then_undo(home):
    """I2 确认卡允许 → 文件出现 → 撤销本轮改动 → 文件消失。"""
    target = home / "proj" / "ui.txt"
    script = [[
        ToolUseBlock(id="w", name="write_file",
                     input={"path": "ui.txt", "content": "来自界面"}),
        TextBlock(text="写好了"),
    ]]
    async with live_server(home, [script]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            await page.fill("#input", "写一个文件")
            await page.keyboard.press("Enter")

            bar = page.locator("#permission-bar")
            await expect(bar).to_be_visible(timeout=15000)
            assert "write_file" in (await page.inner_text("#perm-tool"))
            await page.click('#permission-bar button[data-decision="allow_once"]')
            await expect(bar).to_be_hidden(timeout=15000)

            await expect(page.locator(".checkpoint-bar")).to_be_visible(timeout=15000)
            assert target.exists(), "允许后文件应真实落盘"

            await page.click(".checkpoint-bar button")
            await expect(page.locator(".checkpoint-bar.done")).to_be_visible(timeout=15000)
            for _ in range(50):
                if not target.exists():
                    break
                await page.wait_for_timeout(100)
            assert not target.exists(), "点撤销后文件应被回滚删除"


async def test_i3_narrow_viewport_no_horizontal_overflow(home):
    """I3 390px 手机宽度：不出现致命横向溢出。"""
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base, width=390, height=844) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            overflow = await page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 40, f"窄屏横向溢出 {overflow}px"
