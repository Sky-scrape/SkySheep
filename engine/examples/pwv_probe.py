"""pywebview EdgeChromium 后端探针：创建隐藏窗口 → 执行 JS → 自动销毁。

用于验证本机 GUI 通道可用；正常交付入口是 `skysheep app`。
运行：uv run python examples/pwv_probe.py（约 3 秒退出）
"""

import webview


def probe() -> None:
    try:
        win = webview.windows[0]
        result = win.evaluate_js("1+1")
        print("WINDOW-OK evaluate_js(1+1) =", result)
    except Exception as e:
        print("WINDOW-FAIL", type(e).__name__, str(e)[:200])
    finally:
        try:
            webview.windows[0].destroy()
        except Exception:
            pass


webview.create_window("probe", html="<h1>ok</h1>", width=300, height=200, hidden=True)
webview.start(func=probe, gui="edgechromium")
print("START-RETURNED")
