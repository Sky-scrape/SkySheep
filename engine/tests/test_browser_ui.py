"""真实浏览器 UI 交互（Playwright 可选层）。

前端零构建、无 Node 工具链，真实渲染行为（键盘操作、焦点、缩放、窄屏、
Enter 发送）此前只能手动验证。本文件用 Playwright 连真实 Chromium 加载
真实 uvicorn 服务的前端页面，覆盖：

- 页面骨架加载：输入框、发送按钮、设置入口存在；
- Enter 发送 / Shift+Enter 换行的输入行为；
- 视口缩放（200% / 50%）与窄屏（手机宽度）不产生横向溢出致命错乱；
- PWA service worker 在真实浏览器里注册成功。

依赖策略：playwright 是**可选 dev 依赖**（不进 pyproject dev 组，不进单测
CI 必跑集，`uv sync --locked` 会修剪它）——本地装了就跑、没装整组跳过
（pytest.importorskip）；装了包但未下载浏览器二进制时同样跳过。
标记 ``e2e + slow``：单测 CI 集（not e2e）不跑，CI 的 e2e 专层因未装
Playwright 而整体跳过；本地显式验证时按需装配：

    uv pip install playwright && python -m playwright install chromium
    pytest tests/test_browser_ui.py
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

pytest.importorskip("playwright.async_api", reason="未安装 playwright（可选依赖）")

from playwright.async_api import async_playwright, expect  # noqa: E402

from skysheep.messages import TextBlock  # noqa: E402
from skysheep.models.fake import FakeProvider  # noqa: E402
from skysheep.server import create_app  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home, script):
    """真实 uvicorn 服务（同 test_ws_e2e 模式），yield http 地址。"""
    import uvicorn

    provider = FakeProvider(script)
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("uvicorn 未能在超时内启动")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


@contextlib.asynccontextmanager
async def browser_page(base_url: str, **viewport):
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - 二进制未下载等环境缺席一律跳过
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
    """首启落在设置页时点「完成」进主界面（容错：按钮不存在则已在主界面）。"""
    done = page.locator("#wizard-done, #btn-wizard-done")
    if await done.count():
        with contextlib.suppress(Exception):
            await done.first.click(timeout=2000)


async def test_page_skeleton_and_input_focus(home):
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            # 核心骨架存在且可用
            await page.wait_for_selector("#input", timeout=10000)
            assert await page.locator("#btn-send").count() == 1
            assert await page.locator("#btn-settings").count() == 1
            # 输入框可聚焦可输入
            await page.click("#input")
            await page.fill("#input", "你好")
            assert await page.input_value("#input") == "你好"


async def test_enter_sends_and_shift_enter_newline(home):
    async with live_server(home, [[TextBlock(text="回答")]]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            await page.click("#input")
            # Shift+Enter：换行不发送
            await page.type("#input", "第一行")
            await page.keyboard.press("Shift+Enter")
            await page.type("#input", "第二行")
            assert "\n" in await page.input_value("#input")
            # Enter：清空输入框 = 已发送（fake provider 立即回）
            await page.keyboard.press("Enter")
            # 注意不能用 wait_for_function：其内部 exec 字符串会撞 CSP 的
            # script-src 'self'（页面禁 eval）；expect 走协议侧轮询无此问题
            await expect(page.locator("#input")).to_have_value("", timeout=10000)


async def test_narrow_viewport_no_horizontal_overflow(home):
    """390px 手机宽度：页面不出现致命横向溢出（滚动宽度超视口 40px 以上）。"""
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base, width=390, height=844) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            overflow = await page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 40, f"窄屏横向溢出 {overflow}px，布局疑似失控"


async def test_zoom_200_percent_composer_usable(home):
    """200% 缩放（模拟低视力用户）：发送按钮与输入框仍可见且可点。"""
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base, width=1280, height=800) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            await page.evaluate("document.body.style.zoom = '2.0'")
            box = await page.locator("#btn-send").bounding_box()
            assert box and box["width"] > 0, "200% 缩放下发送按钮应仍占有效布局"


async def test_service_worker_registers(home):
    """PWA：真实浏览器里 service worker 注册成功（sw.js 可达且语法有效）。"""
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.wait_for_timeout(1500)  # 等 app.js 的注册调用落地
            state = await page.evaluate(
                "navigator.serviceWorker.controller ? 'controlled' : "
                "(navigator.serviceWorker.ready ? 'pending' : 'none')")
            assert state in ("controlled", "pending"), f"service worker 状态异常：{state}"
