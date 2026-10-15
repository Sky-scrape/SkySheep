"""WebView2 浏览器参数：从引擎层关掉 Chromium 的「滑动历史导航」。

窗口里只有一个文档（应用页），触屏边缘滑动与触控板横向滑动在 Chromium 里会被当成
「后退 / 前进」历史手势：既要画出那个圆形后退提示，也真的执行一次历史导航——后退
一步就落到启动动画页或空白页，窗口看着像自己往回跳。已有的三层兜底覆盖并不完整：

- WebView2 的 ``IsSwipeNavigationEnabled`` 按官方说明只管「触摸输入设备」（触屏），
  触控板那条路径不在它的范围内；
- 页面 CSS 的 ``overscroll-behavior`` 要看手势落在哪个滚动容器上，而侧栏、会话列表、
  对话列表各自都是滚动容器，只在 ``html, body`` 上写一次盖不住全部落点；
- 启动页自愈与导航看守是「已经退回去了再拉回来」，本来就不拦手势本身。

所以这里在引擎层把该特性整个关掉，与界面结构无关。参数经环境变量
``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` 下发：官方文档明确它的值是**追加**到
``CreateCoreWebView2EnvironmentWithOptions`` 的 ``additionalBrowserArguments`` 上的，
不会顶掉 pywebview 自己设的参数（覆盖语义只有 browserExecutableFolder 与
userDataFolder 那两个变量才是）。

追加会产生两处 ``--disable-features``（pywebview 已经设了一个），命令行按后出现
的那个取值，所以这里把 pywebview 原本关掉的 ``ElasticOverscroll`` 一并写上，免得
把它顶掉。
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

# WebView2 在读环境创建参数时取一次；必须在 webview.start() 之前设好
BROWSER_ARGS_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"

# 特性名取自本机 WebView2 运行时（154.x）二进制里实际存在的特性表：
# OverscrollHistoryNavigation / TouchpadOverscrollHistoryNavigation 是滑动历史导航的
# 两条路径，ElasticOverscroll 是 pywebview 原本就关的橡皮筋回弹。
_DISABLED_FEATURES = (
    "ElasticOverscroll",
    "OverscrollHistoryNavigation",
    "TouchpadOverscrollHistoryNavigation",
)
# 同一功能的旧式命令行开关（0 = 关），认它的内核版本靠它兜底
_SWITCHES = ("--overscroll-history-navigation=0",)


def browser_args() -> str:
    """要追加到 WebView2 启动参数的字符串（纯字符串，方便单独断言）。"""
    features = ",".join(_DISABLED_FEATURES)
    return " ".join(
        (
            *_SWITCHES,
            f"--disable-features={features}",
            f"--disable-blink-features={features}",
        )
    )


def apply_browser_args(env: MutableMapping[str, str] | None = None) -> str:
    """把参数并进 ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS``，返回最终值。

    幂等：同一份参数已经在变量里就不再重复追加（两个启动入口可能都调）。调用方
    要保证时机在 ``webview.start()`` 之前——WebView2 创建环境时只读一次。
    """
    target = os.environ if env is None else env
    extra = browser_args()
    current = (target.get(BROWSER_ARGS_ENV) or "").strip()
    if not current:
        merged = extra
    elif extra in current:
        merged = current
    else:
        merged = f"{current} {extra}"
    target[BROWSER_ARGS_ENV] = merged
    return merged
