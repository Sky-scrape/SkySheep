"""临时缺陷探针：验证若干可疑边界（只读探查，不改项目源码）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TMP = Path(tempfile.mkdtemp(prefix="skysheep-probe-"))
os.environ["SKYSHEEP_HOME"] = str(TMP / "home")
(TMP / "proj").mkdir()

from fastapi.testclient import TestClient  # noqa: E402

from skysheep.messages import TextBlock, ToolUseBlock  # noqa: E402
from skysheep.models.fake import FakeProvider  # noqa: E402
from skysheep.server import create_app  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + detail if detail else ""))


def make_client(script=None):
    provider = FakeProvider(script or [])
    app = create_app(
        working_dir=TMP / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    return TestClient(app)


def recv_until(ws, wanted_id=None, events=None):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


def call(ws, mid, method, params=None):
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    return recv_until(ws, mid)


def short(r: dict) -> str:
    return json.dumps(r.get("result") if r.get("ok") else r.get("error"), ensure_ascii=False)[:200]


def probe_save_provider_kind() -> None:
    """BUG-A: WS config.save_provider 是否转发 kind（设置页「供应商类型」下拉）。"""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        r = call(ws, "a1", "config.add_provider", {
            "name": "relayx", "kind": "openai",
            "base_url": "https://relay.example.com/v1",
            "model": "m1", "api_key": "sk-test",
        })
        check("A0 添加自定义服务", r["ok"], short(r))
        r = call(ws, "a2", "config.save_provider", {
            "name": "relayx", "kind": "anthropic", "model": "m2",
        })
        check("A1 save_provider 调用成功", r["ok"], short(r))
        r = call(ws, "a3", "config.providers")
        provs = {p["name"]: p for p in r["result"]["providers"]}
        actual = provs.get("relayx", {}).get("kind")
        check(
            "A2 协议类型被保存为 anthropic",
            actual == "anthropic",
            f"实际落库 kind={actual}（期望 anthropic）",
        )


def probe_session_search_escape() -> None:
    """BUG-B: 会话搜索 query 含 LIKE 通配符（% _）是否按字面量处理。"""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        check("B0 写入含 % 的消息", call(ws, "b1", "chat.send", {"text": "百分比 50% 完成"})["ok"])
        check("B1 写入普通消息", call(ws, "b2", "chat.send", {"text": "纯文本 abc 完成"})["ok"])
        r = call(ws, "b3", "session.search", {"query": "%"})
        got = len(r["result"]["results"]) if r["ok"] else -1
        check("B2 搜索 '%' 只命中含百分号的消息", got == 1,
              f"命中 {got} 条（期望 1；=2 说明 LIKE 通配符未转义）")
        r = call(ws, "b4", "session.search", {"query": "_"})
        got = len(r["result"]["results"]) if r["ok"] else -1
        check("B3 搜索 '_' 不命中任意单字符", got == 0, f"命中 {got} 条（期望 0）")


def probe_empty_search() -> None:
    """BUG-C: 空 query 是否返回全部会话。"""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        call(ws, "c0", "chat.send", {"text": "hello world"})
        r = call(ws, "c1", "session.search", {"query": ""})
        got = len(r["result"]["results"]) if r["ok"] else -1
        check("C1 空 query 不返回全部结果", got == 0, f"命中 {got} 条（期望 0）")


def probe_export_special_title() -> None:
    """BUG-D: 标题含路径字符时导出文件名是否安全。"""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        r = call(ws, "d1", "chat.send", {"text": "标题测试"})
        sid = r["result"]["session_id"] if r["ok"] else None
        call(ws, "d2", "session.rename", {"id": sid, "title": "../../evil\nline2 测试"})
        r = call(ws, "d3", "session.export", {"id": sid})
        fn = r["result"]["filename"] if r["ok"] else ""
        bad = ("/" in fn) or ("\\" in fn) or (".." in fn) or ("\n" in fn)
        check("D1 导出文件名不含路径字符", not bad, f"filename={fn!r}")


def probe_rename_whitespace() -> None:
    """BUG-E: 重命名只填空白是否被拒。"""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        r = call(ws, "e1", "chat.send", {"text": "x"})
        sid = r["result"]["session_id"]
        r = call(ws, "e2", "session.rename", {"id": sid, "title": "   "})
        check("E1 空白标题被拒绝", not r["ok"], f"ok={r['ok']} err={r.get('error')}")


def probe_ui_prefs() -> None:
    """BUG-F: ui.save 未知键 / 坏类型 / 越界是否安全收敛。"""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        r = call(ws, "f1", "ui.save", {"prefs": {"evil": 1, "sidebar_w": 99999}})
        prefs = r["result"]["prefs"] if r["ok"] else {}
        check("F1 未知键被忽略", "evil" not in prefs, f"prefs={prefs}")
        check("F2 越界值收敛到上限", prefs.get("sidebar_w") == 460, f"sidebar_w={prefs.get('sidebar_w')}")
        r = call(ws, "f2", "ui.save", {"prefs": {"sidebar_w": "abc"}})
        prefs2 = r["result"]["prefs"] if r["ok"] else {}
        check("F3 坏类型不破坏已有值", prefs2.get("sidebar_w") == 460, f"prefs={prefs2}")
        r = call(ws, "f3", "ui.save", {"prefs": {"right_tabs": ["aux", "bogus", "aux"]}})
        prefs3 = r["result"]["prefs"] if r["ok"] else {}
        check("F4 right_tabs 去重+白名单", prefs3.get("right_tabs") == ["aux"], f"{prefs3.get('right_tabs')}")


def probe_terminal() -> None:
    """BUG-G: 终端空命令 / 输出回显 / 退出码。"""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        r = call(ws, "g1", "term.run", {"command": "   "})
        check("G1 空命令被拒绝", not r["ok"], f"err={r.get('error')}")
        events: list = []
        ws.send_json({"id": "g2", "method": "term.run", "params": {"command": "echo hello-term"}})
        r = recv_until(ws, "g2", events)
        text = "".join(e["data"].get("text", "") for e in events if e["event"] == "terminal_chunk")
        check("G2 终端输出回显", bool(r.get("ok")) and "hello-term" in text, f"text={text!r}")
        check("G3 终端退出码 0", bool(r.get("ok")) and (r.get("result") or {}).get("code") == 0,
              f"result={r.get('result')}")


def probe_checkpoint() -> None:
    """BUG-H: 检查点生成 + 重复还原是否幂等。"""
    script = [
        [ToolUseBlock(id="t1", name="write_file", input={"path": "cp.txt", "content": "v1"})],
        [TextBlock(text="done")],
    ]
    with make_client(script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "h1", "method": "chat.send", "params": {"text": "写文件"}})
        result_chk = None
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                if frame["event"] == "permission_request":
                    ws.send_json({
                        "id": "hp", "method": "permission.respond",
                        "params": {"request_id": frame["data"]["request_id"], "decision": "allow_once"},
                    })
                continue
            if frame.get("id") == "h1":
                result_chk = (frame.get("result") or {}).get("checkpoint")
                break
        check("H0 本轮生成检查点", bool(result_chk), f"checkpoint={result_chk}")
        r = call(ws, "h2", "checkpoint.list")
        cps = r["result"]["checkpoints"]
        if not cps:
            check("H1 检查点可列出", False, "列表为空")
            return
        cid = cps[0]["id"]
        r1 = call(ws, "h3", "checkpoint.restore", {"id": cid})
        r2 = call(ws, "h4", "checkpoint.restore", {"id": cid})
        check("H1 重复还原幂等", r1["ok"] and r2["ok"], f"r1={r1['ok']} r2={r2['ok']} err={r2.get('error')}")
        # 还原后文件应已删除
        check("H2 还原后新建文件消失", not (TMP / "proj" / "cp.txt").exists(), "cp.txt 仍存在")


def probe_todo_write() -> None:
    """BUG-J: todo_write 清空列表（items=[]）是否同步到前端事件。"""
    script = [
        [ToolUseBlock(id="t1", name="todo_write", input={"items": [
            {"text": "第一步", "status": "pending"}, {"text": "第二步", "status": "pending"}]})],
        [ToolUseBlock(id="t2", name="todo_write", input={"items": []})],
        [TextBlock(text="清空完成")],
    ]
    with make_client(script) as client, client.websocket_connect("/ws") as ws:
        events: list = []
        ws.send_json({"id": "j1", "method": "chat.send", "params": {"text": "建清单再清空"}})
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                events.append(frame)
                if frame["event"] == "permission_request":
                    ws.send_json({
                        "id": "jp", "method": "permission.respond",
                        "params": {"request_id": frame["data"]["request_id"], "decision": "allow_once"},
                    })
                continue
            if frame.get("id") == "j1":
                break
        todos_events = [e for e in events if e["event"] == "todo_updated"]
        check("J1 todo_updated 事件发生", len(todos_events) >= 1, f"次数={len(todos_events)}")
        if todos_events:
            last = todos_events[-1]["data"].get("items")
            check("J2 最后一次事件 items 为空", last == [], f"items={last}")
        r = call(ws, "j2", "chat.status")
        check("J3 status 里 todos 为空", (r["result"] or {}).get("todos") == [],
              f"todos={(r['result'] or {}).get('todos')}")


def probe_path_escape() -> None:
    """BUG-I: 工具路径越界（记录现状，判断是否符合预期安全模型）。"""
    from skysheep.tools.base import ToolContext, resolve_path

    ctx = ToolContext(working_dir=TMP / "proj")
    p = resolve_path(ctx, "../../../../../../Windows/System32/drivers/etc/hosts")
    inside = str(p).startswith(str((TMP / "proj").resolve()))
    check("I1 路径越界现状记录", True, f"resolved={p} inside_workspace={inside}")


def main() -> None:
    for fn in (
        probe_save_provider_kind,
        probe_session_search_escape,
        probe_empty_search,
        probe_export_special_title,
        probe_rename_whitespace,
        probe_ui_prefs,
        probe_terminal,
        probe_checkpoint,
        probe_todo_write,
        probe_path_escape,
    ):
        print("\n---- " + fn.__name__ + " ----")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            check(fn.__name__ + " 执行异常", False, f"{type(e).__name__}: {e}")

    fails = [r for r in RESULTS if not r[1]]
    print("\n==== 摘要 ====")
    print(f"共 {len(RESULTS)} 项断言，失败 {len(fails)} 项")
    for name, _, detail in fails:
        print("  FAIL " + name + " | " + detail)


if __name__ == "__main__":
    main()
