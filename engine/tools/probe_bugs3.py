"""临时探针 3：会话恢复后 system 提示词是否还在 + 更多生命周期边界。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TMP = Path(tempfile.mkdtemp(prefix="probe3-"))
os.environ["SKYSHEEP_HOME"] = str(TMP / "home")
(TMP / "proj").mkdir()

from fastapi.testclient import TestClient  # noqa: E402

from skysheep.messages import TextBlock  # noqa: E402
from skysheep.models.fake import FakeProvider  # noqa: E402
from skysheep.server import create_app  # noqa: E402

PROVIDER = FakeProvider([]).with_default([TextBlock(text="ok")])


def mk():
    app = create_app(working_dir=TMP / "proj", provider_name="fake", provider_factory=lambda: PROVIDER)
    return TestClient(app)


def call(ws, mid, m, p=None, ev=None):
    ws.send_json({"id": mid, "method": m, "params": p or {}})
    while True:
        f = ws.receive_json()
        if "event" in f:
            if ev is not None:
                ev.append(f)
            continue
        if f.get("id") == mid:
            return f


print("==== S. 会话恢复后 system 提示词 ====")
# 第一次连接：发一条消息（落库）
with mk() as c, c.websocket_connect("/ws") as ws:
    r = call(ws, "s1", "chat.send", {"text": "第一条消息"})
    sid = r["result"]["session_id"]
    st = call(ws, "s2", "chat.status")
    print("  新会话 status.session_id:", st["result"]["session_id"])
    print("  发送时 provider 收到的消息角色:", [m.role for m in PROVIDER.calls[-1]][:4])

# 模拟重启：新客户端 + 新 backend（同一 DB）
PROVIDER.calls.clear()
with mk() as c, c.websocket_connect("/ws") as ws:
    # 启动时 open_initial_session 会 resume 最近会话；再发一条看模型收到什么
    r = call(ws, "s3", "chat.send", {"text": "第二条消息"})
    roles = [m.role for m in PROVIDER.calls[-1]] if PROVIDER.calls else []
    first_role = roles[0] if roles else None
    has_system = "system" in roles
    print("  重启后本轮模型收到的角色序列:", roles[:6])
    print("  首条是否 system:", first_role)
    print("  是否包含 system:", has_system)
    print("  → 结论:", "正常" if has_system else "★ system 提示词丢失（历史里没有 system）")

    # 显式 resume 一次再确认
    PROVIDER.calls.clear()
    call(ws, "s4", "session.resume", {"id": sid})
    r = call(ws, "s5", "chat.send", {"text": "第三条"})
    roles2 = [m.role for m in PROVIDER.calls[-1]] if PROVIDER.calls else []
    print("  显式 resume 后角色序列:", roles2[:6], " 含 system:", "system" in roles2)

print("\n==== T. 新会话（不 resume）对照 ====")
PROVIDER.calls.clear()
with mk() as c, c.websocket_connect("/ws") as ws:
    call(ws, "t1", "session.new")
    call(ws, "t2", "chat.send", {"text": "全新会话消息"})
    roles = [m.role for m in PROVIDER.calls[-1]] if PROVIDER.calls else []
    print("  新会话角色序列:", roles[:6], " 含 system:", "system" in roles)

print("\n==== U. 数据库里是否持久化 system ====")
import sqlite3  # noqa: E402

db = sqlite3.connect(TMP / "home" / "skysheep.db")
rows = db.execute("SELECT session_id, seq, role FROM messages ORDER BY session_id, seq").fetchall()
for r_ in rows[:12]:
    print("  ", r_)
db.close()

print("\n==== V. 上下文占用统计（含 system 与否的差异）====")
with mk() as c, c.websocket_connect("/ws") as ws:
    st = call(ws, "v1", "chat.status")
    print("  status:", {k: st["result"][k] for k in ("context_tokens", "history_messages", "tool_count")})
