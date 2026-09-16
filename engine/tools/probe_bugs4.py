"""临时探针 4：上下文压缩后的持久化 + 运行中会话操作。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TMP = Path(tempfile.mkdtemp(prefix="probe4-"))
HOME = TMP / "home"
HOME.mkdir()
os.environ["SKYSHEEP_HOME"] = str(HOME)
(TMP / "proj").mkdir()

# 低上下文上限，便于触发压缩
(HOME / "config.toml").write_text(
    'default = "fake"\n'
    "context_limit_tokens = 4000\n"
    "compaction_keep_recent = 2\n"
    '[providers.fake]\nkind = "openai"\nmodel = "fake-1"\napi_key = "x"\n',
    encoding="utf-8",
)

from fastapi.testclient import TestClient  # noqa: E402

from skysheep.messages import TextBlock  # noqa: E402
from skysheep.models.fake import FakeProvider  # noqa: E402
from skysheep.server import create_app  # noqa: E402

BIG = "这是一段用来撑大上下文的长文本。" * 300  # ~4500 字符 ≈ 1285 tokens


def mk():
    p = FakeProvider([]).with_default([TextBlock(text="好的，收到。")])
    app = create_app(working_dir=TMP / "proj", provider_name="fake", provider_factory=lambda: p)
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


print("==== W. 压缩后本轮消息是否落库 ====")
with mk() as c, c.websocket_connect("/ws") as ws:
    sid = None
    for i in range(1, 6):
        ev: list = []
        r = call(ws, f"w{i}", "chat.send", {"text": f"第{i}轮 {BIG}"}, ev)
        sid = (r.get("result") or {}).get("session_id", sid)
        comp = [e for e in ev if e["event"] == "compaction"]
        res = r.get("result") or {}
        print(
            f"  第{i}轮 ok={r.get('ok')} 压缩={'是' if comp else '否'}"
            f" context_tokens={res.get('context_tokens')}"
        )

db = sqlite3.connect(HOME / "skysheep.db")
rows = db.execute(
    "SELECT seq, role, length(content) FROM messages WHERE session_id = ? ORDER BY seq", (sid,)
).fetchall()
db.close()
print("  数据库里该会话消息:", len(rows), "条")
for r_ in rows:
    print("   ", r_)
print("  → 期望：5 轮 = 10 条（user+assistant 各 5）；若明显偏少说明压缩后落库丢失")

print("\n==== X. 运行中的会话操作是否会串会话 ====")
# 用一个慢 provider：第一轮挂起（等待权限），期间做 session.new
from skysheep.messages import ToolUseBlock  # noqa: E402


def mk2():
    p = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file", input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="写完")],
    ])
    app = create_app(working_dir=TMP / "proj", provider_name="fake", provider_factory=lambda: p)
    return TestClient(app)


with mk2() as c, c.websocket_connect("/ws") as ws:
    ws.send_json({"id": "x1", "method": "chat.send", "params": {"text": "写文件"}})
    # 等到权限请求
    sid_a = None
    perm_frame = None
    while True:
        f = ws.receive_json()
        if "event" in f:
            if f["event"] == "permission_request":
                perm_frame = f
                break
            continue
        if f.get("id") == "x1":
            break
    print("  收到权限请求:", bool(perm_frame))
    # 运行中新建会话
    r = call(ws, "x2", "session.new")
    print("  运行中 session.new:", r.get("ok"), (r.get("result") or {}).get("id"))
    # 再放行权限
    if perm_frame:
        ws.send_json({
            "id": "xp", "method": "permission.respond",
            "params": {"request_id": perm_frame["data"]["request_id"], "decision": "allow_once"},
        })
    while True:
        f = ws.receive_json()
        if "event" in f:
            continue
        if f.get("id") == "x1":
            break
    print("  收尾完成")
