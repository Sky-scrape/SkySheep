"""诊断 3：复刻 skysheep app 的线程结构（先起 uvicorn 线程，再 webview.start），
验证是否导致窗口立即返回；并测试修复模式 webview.start(func=...)。

用法：python pwv_probe3.py before   # 复刻现状（先 uvicorn 后 GUI）
      python pwv_probe3.py after    # 修复模式（GUI 先行，func 里起服务）
"""

import sys
import threading
import time

import uvicorn
import webview
from fastapi import FastAPI

mode = sys.argv[1] if len(sys.argv) > 1 else "before"
port = 18973


def killer():
    time.sleep(6)
    try:
        webview.windows[0].destroy()
    except Exception:
        pass


t0 = time.monotonic()
app = FastAPI()


@ app.get("/health")
def health():
    return {"ok": True}


server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))

if mode == "before":
    threading.Thread(target=server.run, daemon=True).start()
    win = webview.create_window("probe3", html="<h1>before</h1>", width=200, height=120, hidden=True)
    threading.Thread(target=killer, daemon=True).start()
    webview.start()
else:
    win = webview.create_window("probe3", html="<h1>after</h1>", width=200, height=120, hidden=True)

    def bg():
        threading.Thread(target=server.run, daemon=True).start()
        time.sleep(1)

    threading.Thread(target=killer, daemon=True).start()
    webview.start(func=bg)

elapsed = time.monotonic() - t0
shown = win.events.shown.is_set()
print(f"RESULT[{mode}]: elapsed={elapsed:.1f}s blocked={elapsed > 4} window_shown={shown}")
