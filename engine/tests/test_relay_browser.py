"""接力（主副屏交替）的真实页面交互：点接力开副屏、看板执行与轮次。

复用可选 Chromium 测试设施；后端用 FakeProvider，不接触真实用户数据。
"""
import pytest

pytest.importorskip("playwright.async_api", reason="未安装 playwright（可选依赖）")

from playwright.async_api import expect  # noqa: E402
from test_browser_ui import _open_main_view, browser_page, live_server  # noqa: E402

from skysheep.messages import TextBlock  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


async def test_relay_opens_peer_pane_and_runs_alternating(home):
    """点左侧「接力」：右侧看板 + 副屏分栏；两屏按轮交替执行。"""
    script = [[TextBlock(text=f"回复 {i}")] for i in range(10)]
    async with live_server(home, script) as base:
        async with browser_page(base, width=1400, height=900) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            # 先发一条普通消息：接力需要一个当前会话
            await page.fill("#input", "你好")
            await page.keyboard.press("Enter")
            await expect(page.locator("#input")).to_have_value("", timeout=10000)

            await page.click('.nav-item[data-act="relay"]')
            await expect(page.locator("#rp-page-relay")).to_be_visible()
            # 副屏会话自动打开在分栏里
            await expect(page.locator("#split-pane .split-title")).to_have_text(
                "接力 · 副屏", timeout=10000
            )
            await expect(page.locator("#relay-peer-chip")).to_contain_text("接力 · 副屏")
            await expect(page.locator("#relay-run-start")).to_be_visible()
            # 看板不再要求先读 JSON：直接填总任务与两屏任务
            assert await page.locator("#rp-page-relay textarea").count() == 3

            await page.fill("#relay-goal", "做一版发布方案并落地")
            await page.fill("#relay-main-task", "发布方案")
            await page.fill("#relay-peer-task", "执行并反馈")
            await page.click("#relay-run-start")
            await expect(page.locator("#relay-run-status")).to_contain_text(
                "轮到副屏", timeout=15000
            )
            assert await page.locator("#relay-turn-list .relay-turn").count() == 1
            await expect(page.locator("#relay-turn-list")).to_contain_text("第 1 轮 · 主屏")

            # 交棒：第二次执行轮到副屏，面板按钮跟着改口
            await expect(page.locator("#relay-run-start")).to_have_text("让副屏执行")
            await page.click("#relay-run-start")
            await expect(page.locator("#relay-run-status")).to_contain_text(
                "轮到主屏", timeout=15000
            )
            assert await page.locator("#relay-turn-list .relay-turn").count() == 2
            await expect(page.locator("#relay-turn-list")).to_contain_text("第 2 轮 · 副屏")

            # 两屏正文都留在各自会话里（副屏分栏里能看到它的产出）
            await expect(page.locator("#split-pane .split-log")).to_contain_text("回复")


async def test_relay_settings_page_is_explainer_and_opens_workbench(home):
    """接力说明卡在「技能与工具」页：只做说明、不带 JSON 表单；入口能回到看板。"""
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.click("#btn-settings")
            # 接力并入了「技能与工具」页（与圆桌 / 团队 / 对抗并列），不再单列导航项
            await page.click('#settings-nav li[data-target="skills"]')
            await expect(page.locator("#relay-config-card")).to_be_visible()
            await expect(page.locator("#relay-config-card")).to_contain_text("主屏与副屏交替推进")
            assert await page.locator("#relay-config-card textarea").count() == 0
            await page.click("#relay-open-workbench")
            await expect(page.locator("#rp-page-relay")).to_be_visible()
            await expect(page.locator("#view-settings")).to_be_hidden()


async def test_relay_nav_keeps_chat_and_panel(home):
    """接力标签与聊天并排：关闭接力标签后聊天仍在。"""
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.click('.nav-item[data-act="relay"]')
            await expect(page.locator("#view-chat")).to_be_visible()
            await expect(page.locator("#rp-page-relay")).to_be_visible()
            await page.locator('#rp-tabs .rp-tab[data-tab-id="relay"] .rp-x').click()
            await expect(page.locator("#view-chat")).to_be_visible()
            await expect(page.locator("#rp-page-relay")).to_be_hidden()


async def test_peer_pane_notices_stay_in_peer(home):
    """副屏里的操作提示写进副屏，不能串到主栏（用户报的串台回归）。"""
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base, width=1400, height=900) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            await page.fill("#input", "你好")
            await page.keyboard.press("Enter")
            await expect(page.locator("#input")).to_have_value("", timeout=10000)
            await page.click('.nav-item[data-act="relay"]')
            await expect(page.locator("#split-pane .split-model")).to_be_visible(timeout=10000)
            await page.click("#split-pane .split-model")
            row = page.locator("#split-model-menu .mm-item").first
            await expect(row).to_be_visible(timeout=10000)
            await row.click()
            await expect(page.locator("#split-pane .split-log")).to_contain_text(
                "这一栏将使用", timeout=10000
            )
            main_text = await page.locator("#chat .chat-log").first.inner_text()
            assert "这一栏将使用" not in main_text
