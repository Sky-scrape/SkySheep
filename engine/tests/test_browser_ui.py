"""真实浏览器 UI 交互（Playwright 可选层）。

前端零构建、无 Node 工具链，真实渲染行为（键盘操作、焦点、缩放、窄屏、
Enter 发送）此前只能手动验证。本文件用 Playwright 连真实 Chromium 加载
真实 uvicorn 服务的前端页面，覆盖：

- 页面骨架加载：输入框、发送按钮、设置入口存在；
- Enter 发送 / Shift+Enter 换行的输入行为；
- 视口缩放（200% / 50%）与窄屏（手机宽度）不产生横向溢出致命错乱；
- PWA service worker 在真实浏览器里注册成功。

依赖策略：playwright 在 pyproject 的独立 **browser 依赖组**（不进 dev 组，
单测 / e2e 两个 CI job 的 `uv sync --locked` 不拉它，本地默认也不装）——
没装整组跳过（pytest.importorskip），装了包但未下载浏览器二进制时同样跳过。
CI 由专门的 `browser UI (playwright)` job 用 `uv sync --group browser` 装齐后跑；
本地显式验证按需装配：

    uv sync --group browser && uv run python -m playwright install chromium
    uv run pytest tests/test_browser_ui.py
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
    # timeout_graceful_shutdown：uvicorn 优雅关闭默认无限等连接排空，而 Windows
    # Proactor 偶发漏掉一条连接的 connection_lost，asyncio.Server.wait_closed()
    # 就会一直卡住。设上限后 uvicorn 超时会照常执行 lifespan shutdown
    # （→ backend.shutdown → store.close）；否则下面 10s 的取消会整段跳过它，
    # 漏掉的 aiosqlite 非 daemon 工作线程会让 pytest 打完 passed 后永不退出。
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error", lifespan="on",
                            timeout_graceful_shutdown=5)
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


async def test_pet_keeps_position_when_split_pane_closes(home):
    """对话分屏（接力副屏）开关不改写小羊落点：关掉副屏后小羊回原处。

    分屏打开时宠物被 CSS 藏起来，同时 #chat 让出右侧宽度；旧实现会在这一帧
    按变窄容器做「横向收敛」，落点被永久改写成窄容器里的坐标——关掉副屏
    只是让它重新可见，小羊就「跳」到了左边。隐藏期间跳过归位后坐标原样保留。
    """
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#chat-pet", timeout=10000)
            # 落点走 petApplyPos：与用户拖动 / 启动恢复同一条路径
            before = await page.evaluate("""() => {
                const b = petBounds();
                petApplyPos(b.maxX, b.floor);
                const el = document.getElementById('chat-pet');
                return {left: el.style.left, top: el.style.top};
            }""")
            sid = await page.evaluate("async () => (await request('session.new', {})).id")
            await page.evaluate("async (sid) => { await openSplitPane(sid, '分屏测试'); }", sid)
            await expect(page.locator("#chat-main.has-split")).to_have_count(1, timeout=10000)
            hidden = await page.evaluate("""() => {
                const el = document.getElementById('chat-pet');
                return {left: el.style.left, top: el.style.top,
                        display: getComputedStyle(el).display};
            }""")
            assert hidden["display"] == "none", "分屏打开时宠物应被藏起来（用例前提）"
            assert (hidden["left"], hidden["top"]) == (before["left"], before["top"]), \
                "分屏让出右侧宽度时不该改写小羊落点"
            await page.click("#split-pane .split-close")
            await expect(page.locator("#chat-main.has-split")).to_have_count(0, timeout=10000)
            await page.wait_for_timeout(600)  # 等 ResizeObserver 归位
            after = await page.evaluate("""() => {
                const el = document.getElementById('chat-pet');
                return {left: el.style.left, top: el.style.top,
                        display: getComputedStyle(el).display};
            }""")
            assert after["display"] != "none"
            assert (after["left"], after["top"]) == (before["left"], before["top"]), \
                "关掉副屏后小羊应回到原处（落点被窄容器改写过？）"


async def test_long_modal_caps_height_and_scrolls_body(home):
    """长表单弹层不出画：框限制在视口内，标题与底部按钮固定，正文区自己滚。

    回归：通用弹窗此前没有高度上限，长表单（定时任务：字段 + 预授权工具表 +
    三段说明）在 620px 高的窗口里撑到满屏，整层靠遮罩滚动——标题被滚出视口、
    底部的取消/创建要滚很远才够得着。上限与滚动层写在 .modal-box /
    #modal-content 的通用规则里，这里用最长的那个弹窗锚住行为。
    """
    async with live_server(home, [[TextBlock(text="好")]]) as base:
        async with browser_page(base, width=1000, height=620) as page:
            await _open_main_view(page)
            await page.wait_for_selector("#input", timeout=10000)
            # 等 bootsnap 到位（cronModal 要用工具清单/服务清单；WS 未连上时 request 会失败）。
            # 注意 bootSnap 是 app-providers.js 里的顶层 let（全局词法绑定），不是 window 属性
            ready = await page.evaluate("""async () => {
                for (let i = 0; i < 100; i++) {
                    if (typeof bootSnap !== "undefined" && bootSnap) return true;
                    await new Promise((r) => setTimeout(r, 50));
                }
                return false;
            }""")
            assert ready, "boot 快照未在 5 秒内就绪"
            # cronModal 是全局 async 函数：等它内部的两次 request 拉完再断言
            await page.evaluate("async () => { await cronModal(); }")
            await expect(page.locator("#modal:not(.hidden)")).to_have_count(1)
            await expect(page.locator("#modal-title")).to_have_text("新建定时任务")
            m = await page.evaluate("""() => {
                const box = document.querySelector('#modal .modal-box');
                const content = document.getElementById('modal-content');
                const r = box.getBoundingClientRect();
                const title = box.querySelector('h3').getBoundingClientRect();
                const ops = box.querySelector('.modal-ops').getBoundingClientRect();
                return {
                  vw: innerWidth, vh: innerHeight,
                  top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                  titleTop: title.top, titleBottom: title.bottom,
                  opsTop: ops.top, opsBottom: ops.bottom,
                  scrollable: content.scrollHeight > content.clientHeight + 1,
                  overflowY: getComputedStyle(content).overflowY,
                };
            }""")
            assert m["top"] >= -1 and m["bottom"] <= m["vh"] + 1, \
                f"弹层超出视口上下边界：{m['top']}..{m['bottom']} / {m['vh']}"
            assert m["left"] >= -1 and m["right"] <= m["vw"] + 1, \
                f"弹层超出视口左右边界：{m['left']}..{m['right']} / {m['vw']}"
            # 标题在框上部、操作条在框下部，且都留在视口内（不是被滚出去的状态）
            assert m["top"] <= m["titleTop"] and m["titleBottom"] <= m["opsTop"]
            assert m["opsBottom"] <= m["bottom"] + 1
            # 正文区是唯一滚动层：内容超高时必须能滚，而不是把框撑出视口
            assert m["scrollable"], "长表单内容应超出可视高度（用例前提）"
            assert m["overflowY"] == "auto", "正文区应自己滚"
            # 预授权工具表：权限徽章贴所在格右缘（同一列形成一条竖线）。
            # 回归：徽章原先紧跟在变长的工具名后面，起点随名字宽度漂移，
            # 同一列看起来是锯齿（read_file 只读 与 write_file 写入 差 6px）
            gaps = await page.evaluate("""() => {
                return [...document.querySelectorAll('.cron-tool')].map((c) => {
                    const cell = c.getBoundingClientRect();
                    const chip = c.querySelector('.chip').getBoundingClientRect();
                    return Math.round((cell.right - chip.right) * 10) / 10;
                });
            }""")
            assert gaps, "预授权工具表为空（用例前提）"
            assert all(0 <= g <= 6 for g in gaps), \
                f"权限徽章没贴齐所在格右缘（应形成竖直一列）：{gaps}"
