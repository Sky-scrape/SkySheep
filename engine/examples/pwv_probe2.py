"""诊断：webview.start() 不指定 gui 参数时是否立即返回（对比 edgechromium）。"""

import sys
import threading

import webview

mode = sys.argv[1] if len(sys.argv) > 1 else "default"
returned = {"flag": False}


def killer():
    import time

    time.sleep(6)
    try:
        webview.windows[0].destroy()
    except Exception:
        pass


threading.Thread(target=killer, daemon=True).start()

win = webview.create_window("probe-" + mode, html="<h1>t</h1>", width=200, height=120, hidden=True)

if mode == "edgechromium":
    webview.start(gui="edgechromium")
else:
    webview.start()

print("RESULT[{}]: start returned (blocked={} window_shown={})".format(
    mode, not returned["flag"], win.events.shown.is_set()
))
