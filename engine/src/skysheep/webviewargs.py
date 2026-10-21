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
import shutil
import threading
import time
from collections.abc import MutableMapping
from pathlib import Path

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

# 内置浏览器忽略 HTTPS 证书校验（设置 · 浏览器控制）：Chromium 官方开关，WebView2
# 照单接收。只在用户显式打开时追加——默认关，不能变成默认姿态。
_IGNORE_CERT_SWITCH = "--ignore-certificate-errors"

# ---- 浏览器数据的启动前落地 ----
# 「清除全部浏览器数据」在使用中删不掉：Cookie / Local Storage 这些是被运行中的
# WebView2 打开的 SQLite 与文件，Windows 上直接删会 PermissionError（应用窗口用的
# 就是这份 profile）。所以清除动作只在数据目录写一个标记，真正的删除在下次启动、
# WebView2 创建环境**之前**落地——这一刻还没有任何东西占用这些文件。
# 本模块只依赖标准库与 instance（同为零依赖）：它跑在窗口之前的首帧路径上，
# 这里 import config 会把 pydantic 拖进首屏。
RESET_MARKER_NAME = "browser-reset.json"
# 应用真正使用的两份 WebView2 profile（桌面壳与 --browser 模式的 `skysheep app`）。
# 不扫 webview* 通配：数据目录里可能有别的工具留下的目录，删除动作只认名单。
PROFILE_DIRS = ("webview", "webview-app")
# WebView2 在 userDataFolder 下自己建的 profile 根目录
PROFILE_INNER = "EBWebView"


def browser_args(ignore_cert: bool = False) -> str:
    """要追加到 WebView2 启动参数的字符串（纯字符串，方便单独断言）。"""
    features = ",".join(_DISABLED_FEATURES)
    parts = [
        *_SWITCHES,
        f"--disable-features={features}",
        f"--disable-blink-features={features}",
    ]
    if ignore_cert:
        parts.append(_IGNORE_CERT_SWITCH)
    return " ".join(parts)


def apply_browser_args(
    env: MutableMapping[str, str] | None = None, *, ignore_cert: bool | None = None
) -> str:
    """把参数并进 ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS``，返回最终值。

    幂等：同一份参数已经在变量里就不再重复追加（两个启动入口可能都调）。调用方
    要保证时机在 ``webview.start()`` 之前——WebView2 创建环境时只读一次。

    ``ignore_cert`` 不传时按 config.toml 的 ``browser_ignore_cert`` 取（见
    :func:`read_ignore_cert`）；显式传值便于测试与调用方覆盖。
    """
    target = os.environ if env is None else env
    if ignore_cert is None:
        ignore_cert = read_ignore_cert()
    extra = browser_args(ignore_cert=ignore_cert)
    current = (target.get(BROWSER_ARGS_ENV) or "").strip()
    if not current:
        merged = extra
    elif extra in current:
        merged = current
    else:
        merged = f"{current} {extra}"
    target[BROWSER_ARGS_ENV] = merged
    return merged


def data_home() -> Path:
    """数据目录（与 config.skysheep_home 同一口径，但不 import config）。

    实例身份经 :mod:`skysheep.instance` 取——那个模块只依赖标准库，可以安全地
    在首帧路径上导入。
    """
    env = os.environ.get("SKYSHEEP_HOME")
    if env:
        return Path(env).expanduser()
    from .instance import DEFAULT_HOME_NAME, instance_name

    name = instance_name()
    return Path.home() / (DEFAULT_HOME_NAME if name is None else f"{DEFAULT_HOME_NAME}-{name}")


def read_ignore_cert(home: Path | None = None) -> bool:
    """只读 config.toml 里的 ``browser_ignore_cert``（tomllib，不 import config）。

    读不到、读坏了、键不在：一律 False——启动路径上的开关不该因为配置异常而
    打不开窗口，默认姿态（校验证书）本身就是安全的那一侧。
    """
    path = (home or data_home()) / "config.toml"
    try:
        import tomllib

        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, ValueError, ImportError):
        return False
    return bool(raw.get("browser_ignore_cert", False))


def _profile_roots(home: Path) -> list[Path]:
    roots: list[Path] = []
    for name in PROFILE_DIRS:
        base = home / name
        inner = base / PROFILE_INNER
        roots.append(inner if inner.is_dir() else base)
    return roots


def apply_pending_reset(home: Path | None = None) -> list[Path]:
    """把「清除全部浏览器数据」的待办落地：**改名前** WebView2 还没接手 profile。

    刻意用改名（同一卷上的 ``os.replace``，瞬时）而不是直接删除：删 50MB 的
    profile 要几百毫秒到几秒，而这一步在窗口出现之前——改名让路径立刻可用，
    真正的删除交给后台线程，首帧不被拖慢。返回被改名（= 已让位）的目录列表。

    标记文件在读到时立刻删掉：改名成功就完成了它交代的事；改名失败（没这个
    目录）同样不该让标记一直挂着、每次启动都重试一遍。
    """
    base_home = home or data_home()
    marker = base_home / RESET_MARKER_NAME
    if not marker.is_file():
        return []
    try:
        marker.unlink()
    except OSError:
        pass
    stamp = str(int(time.time()))
    renamed: list[Path] = []
    for root in _profile_roots(base_home):
        if not root.is_dir():
            continue
        target = root.with_name(f"{root.name}.old-{stamp}")
        try:
            os.replace(root, target)
        except OSError:
            continue  # 被占用 / 权限不足：留着，用户可再来一次
        renamed.append(target)
    if renamed:
        # 后台删除：daemon + 自吞异常——清不掉（杀软锁住单个文件）也不该
        # 影响应用，下次启动的清理会再扫一遍 .old-* 残留。
        threading.Thread(target=_purge_old_dirs, args=(base_home,), daemon=True).start()
    return renamed


def _purge_old_dirs(home: Path) -> None:
    """删掉改名留下的 ``*.old-*`` 目录（含上次启动没删完的残留）。"""
    for name in PROFILE_DIRS:
        base = home / name
        try:
            leftovers = list(base.glob("*.old-*"))
        except OSError:
            continue
        for d in leftovers:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

