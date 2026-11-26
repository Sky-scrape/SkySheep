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
    # live_server 给的是 ws://host:port/ws（那是给 WS 客户端用的）；浏览器要的是
    # 同一个服务的 http 根地址。此前直接把 ws:// 交给 page.goto，Chromium 报
    # ERR_UNKNOWN_URL_SCHEME——这组用例一直没在装过 playwright 的环境里跑，所以
    # 没被发现（CI 的 browser job 只跑 tests/test_browser_ui.py 与 test_relay_browser.py）。
    url = base_url
    if url.startswith("ws://") or url.startswith("wss://"):
        url = url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
        url = url.rsplit("/ws", 1)[0]
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - 二进制没下载就跳过
            raise pytest.skip.Exception(
                f"Chromium 不可用（先跑 playwright install chromium）：{e}"
            ) from e
        page = await browser.new_page(viewport=viewport or {"width": 1280, "height": 800})
        await page.goto(url, wait_until="domcontentloaded")
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


async def test_i4_right_panel_tab_bar_aligns_with_topbar(home):
    """I4 右面板标签条与顶栏同高：并排两列的顶部横线落在同一条水平线上。

    标签条高度由标签内容撑出（约 41px），比顶栏实高矮一截；app.js 的
    syncRpTabsHeight 按 topbar.offsetHeight 实测同步。这里量两条线的实际
    bottom，防止后续改标签栏样式或删掉同步后两条线又错开。
    """
    async with live_server(home, []) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            await page.click("#btn-tabs")
            await page.wait_for_selector("#rp-tabs .rp-tab", timeout=10000)
            bottoms = await page.evaluate(
                "() => [document.querySelector('#topbar').getBoundingClientRect().bottom,"
                " document.querySelector('#rp-tabs').getBoundingClientRect().bottom]"
            )
            assert abs(bottoms[0] - bottoms[1]) < 1.5, bottoms


async def test_i5_split_composer_aligns_with_main(home):
    """I5 副屏首次出现：主栏与分屏列输入区的顶线对齐，分屏顶部对到顶栏下沿。

    分屏列输入区此前按自己的默认高度铺（比主栏输入区矮十几像素），分屏
    顶部也停在 CSS 的 44px 兜底上；openSplitPane 现在在列入册后同步顶部，
    并在未拖过分屏输入区时按主栏输入区高度起步。
    """
    async with live_server(home, []) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            await page.evaluate(
                "async () => { const r = await request('session.new');"
                " await openSplitPane(r.id, r.title); }"
            )
            await page.wait_for_selector(".split-composer", timeout=10000)
            boxes = await page.evaluate(
                "() => {"
                " const c = document.querySelector('#composer').getBoundingClientRect();"
                " const s = document.querySelector('.split-composer').getBoundingClientRect();"
                " const t = document.querySelector('#topbar').getBoundingClientRect();"
                " const p = document.querySelector('#split-pane').getBoundingClientRect();"
                " return {mainTop: c.top, splitTop: s.top,"
                "         topbarBottom: t.bottom, splitPaneTop: p.top};"
                " }"
            )
            assert abs(boxes["mainTop"] - boxes["splitTop"]) < 1.5, boxes
            assert abs(boxes["topbarBottom"] - boxes["splitPaneTop"]) < 1.5, boxes
