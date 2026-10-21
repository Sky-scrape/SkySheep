"""WebView2 滑动历史导航开关（见 src/skysheep/webviewargs.py）。

这里守三件事：

1. **两个窗口入口都要设上**。``desktop.py``（安装版 / SkySheep.pyw）与
   ``cli/app.py``（``skysheep app`` / 启动SkySheep.bat）各建各的 WebView2，
   漏掉哪一条，那条路径上触控板横滑就照旧会画出圆形后退提示、并真的后退一次。
2. **追加语义**。官方文档写明 ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` 的值是
   追加到 ``additionalBrowserArguments`` 上的，所以不能把用户/其他组件已经放进去的
   参数顶掉——尤其不能丢掉 pywebview 自己设的那份。
3. **幂等**。同一进程里两个入口都可能调，重复调用不该把参数叠成一长串。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from skysheep import webviewargs

_ENGINE = Path(__file__).resolve().parents[1]


def test_args_cover_both_gesture_paths():
    """触屏与触控板是两条路径，两个特性名都要在；旧式命令行开关一并兜底。"""
    args = webviewargs.browser_args()
    assert "--overscroll-history-navigation=0" in args
    assert "OverscrollHistoryNavigation" in args
    assert "TouchpadOverscrollHistoryNavigation" in args
    assert "--disable-features=" in args
    assert "--disable-blink-features=" in args


def test_args_keep_pywebview_elastic_overscroll():
    """pywebview 原本关着 ElasticOverscroll；追加的两个 --disable-features 后者生效，
    所以这份参数里必须把 ElasticOverscroll 再写一遍，否则等于把它打开了。"""
    assert "ElasticOverscroll" in webviewargs.browser_args()


def test_apply_sets_env_when_empty():
    env: dict[str, str] = {}
    merged = webviewargs.apply_browser_args(env)
    assert env[webviewargs.BROWSER_ARGS_ENV] == merged == webviewargs.browser_args()


def test_apply_appends_to_existing_value():
    """已有参数原样保留（追加而不是覆盖），新参数接在后面。"""
    env = {webviewargs.BROWSER_ARGS_ENV: "--allow-file-access-from-files"}
    merged = webviewargs.apply_browser_args(env)
    assert merged.startswith("--allow-file-access-from-files ")
    assert env[webviewargs.BROWSER_ARGS_ENV] == merged
    assert "TouchpadOverscrollHistoryNavigation" in merged


def test_apply_is_idempotent():
    env: dict[str, str] = {}
    once = webviewargs.apply_browser_args(env)
    twice = webviewargs.apply_browser_args(env)
    assert once == twice
    assert twice.count("--overscroll-history-navigation=0") == 1


def test_apply_does_not_duplicate_args_set_by_someone_else():
    """外部已经设过同一份参数（例如打包环境里预置）时不重复追加。"""
    env = {webviewargs.BROWSER_ARGS_ENV: f"--x {webviewargs.browser_args()}"}
    assert webviewargs.apply_browser_args(env) == f"--x {webviewargs.browser_args()}"


def test_both_window_entries_apply_the_args():
    """两个入口的源码里都要真调到（漏一个入口＝那条路径上问题照旧）。"""
    desktop = (_ENGINE / "desktop.py").read_text(encoding="utf-8")
    cli_app = (_ENGINE / "src" / "skysheep" / "cli" / "app.py").read_text(encoding="utf-8")
    for name, source in (("desktop.py", desktop), ("cli/app.py", cli_app)):
        assert "apply_browser_args()" in source, f"{name} 没设 WebView2 滑动导航参数"
        # 「清除全部浏览器数据」的落地同样必须在两个入口、且在窗口之前：漏一个入口，
        # 那条路径上排队的清除就永远不会执行（使用中删不掉 Cookie）
        assert "apply_pending_reset()" in source, f"{name} 没落地排队的浏览器数据清除"


def test_webviewargs_module_is_importable_without_heavy_deps():
    """desktop.py 靠它赶在 webview.start() 之前设环境变量，所以它自身不能拖引擎。"""
    spec = importlib.util.spec_from_file_location(
        "skysheep_webviewargs_standalone", _ENGINE / "src" / "skysheep" / "webviewargs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.browser_args() == webviewargs.browser_args()
