"""临时探针 2：确认 save_provider 的 kind 丢失、搜索误命中、todo 事件。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TMP = Path(tempfile.mkdtemp(prefix="probe2-"))
os.environ["SKYSHEEP_HOME"] = str(TMP / "home")
(TMP / "proj").mkdir()

from fastapi.testclient import TestClient  # noqa: E402

from skysheep.config import config_path  # noqa: E402
from skysheep.messages import TextBlock, ToolUseBlock  # noqa: E402
from skysheep.models.fake import FakeProvider  # noqa: E402
from skysheep.server import create_app  # noqa: E402


def mk(script=None):
    p = FakeProvider(script or [])
    app = create_app(working_dir=TMP / "proj", provider_name="fake", provider_factory=lambda: p)
    return TestClient(app)


def recv(ws, mid=None, ev=None):
    while True:
        f = ws.receive_json()
        if "event" in f:
            if ev is not None:
                ev.append(f)
            continue
        if mid is None or f.get("id") == mid:
            return f


def call(ws, mid, m, p=None):
    ws.send_json({"id": mid, "method": m, "params": p or {}})
    return recv(ws, mid)


print("==== A. save_provider 的 kind 是否落盘 ====")
with mk() as c, c.websocket_connect("/ws") as ws:
    call(ws, "a1", "config.add_provider", {
        "name": "relayx", "kind": "openai",
        "base_url": "https://relay.example.com/v1", "model": "m1", "api_key": "sk-test",
    })
    r = call(ws, "a2", "config.save_provider", {"name": "relayx", "kind": "anthropic", "model": "m2"})
    print("save_provider ok:", r.get("ok"), r.get("error") or "")
    r = call(ws, "a3", "config.providers")
    provs = r["result"]["providers"]
    print("providers_detail kind:", provs["relayx"]["kind"], "  期望 anthropic")
    raw = config_path().read_text(encoding="utf-8")
    sec = raw.split("[providers.relayx]")[1] if "[providers.relayx]" in raw else ""
    print("config.toml 段落:", sec.strip().replace("\n", " | ")[:160])

print("\n==== B. 搜索命中 JSON 元数据字段而非正文 ====")
with mk() as c, c.websocket_connect("/ws") as ws:
    call(ws, "b0", "chat.send", {"text": "纯文本 abc"})
    for q in ["_", "role", "created_at", "type", "tool_use_id", "abc"]:
        r = call(ws, "bq", "session.search", {"query": q})
        res = r["result"]["results"]
        snip = res[0]["snippet"] if res else ""
        print(f"  query={q!r:14} 命中 {len(res)} 条  snippet={snip[:70]!r}")

print("\n==== C. 空 query ====")
with mk() as c, c.websocket_connect("/ws") as ws:
    r = call(ws, "c1", "session.search", {"query": ""})
    print("  空 query 命中:", len(r["result"]["results"]))

print("\n==== J. todo_write（正确参数名 todos）====")
script = [
    [ToolUseBlock(id="t1", name="todo_write", input={"todos": [
        {"content": "第一步", "status": "pending"}, {"content": "第二步", "status": "pending"}]})],
    [ToolUseBlock(id="t2", name="todo_write", input={"todos": []})],
    [TextBlock(text="清空完成")],
]
with mk(script) as c, c.websocket_connect("/ws") as ws:
    ev: list = []
    ws.send_json({"id": "j1", "method": "chat.send", "params": {"text": "清单"}})
    while True:
        f = ws.receive_json()
        if "event" in f:
            ev.append(f)
            continue
        if f.get("id") == "j1":
            break
    tu = [e for e in ev if e["event"] == "todo_updated"]
    print("  todo_updated 次数:", len(tu))
    for e in tu:
        print("   items:", e["data"]["items"])
    r = call(ws, "j2", "chat.status")
    print("  status.todos:", r["result"]["todos"])
