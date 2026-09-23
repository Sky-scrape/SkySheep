"""M4 对标主流 Agent 的新功能测试：

- turn 级瞬态错误自动重试（对标 Claude Code / Codex 的 auto-retry）
- web_fetch 联网工具（SSRF 防护 + HTML 转文本 + 重定向校验）
- 检查点/回滚（write/edit 改前快照，一键撤销本轮改动）
- 消息排队（Agent 工作时继续发消息，本轮结束后自动执行）
- /status /compact、fs.files（@ 提及数据源）、session.search（跨会话搜索）
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeProvider
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.core import Agent
from skysheep.core.agent import is_transient_error
from skysheep.core.checkpoints import CheckpointStore
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.base import ProviderTextDelta
from skysheep.security.gate import PermissionGate
from skysheep.tools import ChangeRecorder, ToolContext, ToolError, ToolRegistry, default_tools
from skysheep.tools.fs import EditFileArgs, WriteFileArgs
from skysheep.tools.web import WebFetchArgs, WebFetchTool, html_to_text


def make_agent(provider, tmp_path):
    gate = PermissionGate(store=None, project_id=None)
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=gate,
        working_dir=tmp_path,
    )


async def collect(agent, text):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request":
            agent.respond_permission(ev.request_id, "allow_once")
    return events


# ---- 瞬态错误自动重试 ----


class FlakyProvider(FakeProvider):
    """前 N 次 stream() 抛瞬态错误，之后正常。"""

    def __init__(self, scripted, failures: int):
        super().__init__(scripted)
        self.failures = failures
        self.calls_made = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls_made += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("Error code: 429 - rate limit exceeded, please retry")
        async for ev in super().stream(messages, tool_schemas):
            yield ev


async def test_transient_error_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("skysheep.core.agent.RETRY_BASE_DELAY_S", 0.0)
    provider = FlakyProvider([[TextBlock(text="终于成功")]], failures=2)
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "hi")

    kinds = [e.kind for e in events]
    assert kinds.count("notice") == 2, "两次失败各发一次重试提示"
    assert "text_delta" in kinds
    assert kinds[-1] == "turn_finished"
    assert events[-1].stop_reason == "end_turn"
    assert provider.calls_made == 3
    assert agent.history[-1].text == "终于成功"


class BoomProvider(FakeProvider):
    async def stream(self, messages, tool_schemas, effort=None):
        raise RuntimeError("invalid api key (401)")
        yield  # noqa: B901 不可达；仅为把函数变成异步生成器（与 Provider 协议一致）


async def test_non_transient_error_no_retry(tmp_path):
    provider = BoomProvider([[TextBlock(text="x")]])
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "hi")
    kinds = [e.kind for e in events]
    assert "error" in kinds
    assert "notice" not in kinds
    assert events[-1].stop_reason == "error"


class MidStreamFailProvider(FakeProvider):
    async def stream(self, messages, tool_schemas, effort=None):
        yield ProviderTextDelta("已经吐了一半 ")
        raise RuntimeError("429 rate limited mid-stream")


async def test_midstream_failure_does_not_replay(tmp_path):
    provider = MidStreamFailProvider([[TextBlock(text="x")]])
    agent = make_agent(provider, tmp_path)
    events = await collect(agent, "hi")
    kinds = [e.kind for e in events]
    assert "error" in kinds
    assert "notice" not in kinds, "已有内容时禁止重放"
    assert events[-1].stop_reason == "error"


def test_is_transient_error_classification():
    assert is_transient_error(RuntimeError("HTTP 503 Service Unavailable"))
    assert is_transient_error(RuntimeError("Connection reset by peer"))
    assert is_transient_error(RuntimeError("Request timed out"))
    assert not is_transient_error(RuntimeError("invalid api key"))
    assert not is_transient_error(RuntimeError("model not found"))


# ---- web_fetch ----


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音测试输出
        pass

    def do_GET(self):
        if self.path == "/html":
            body = (
                b"<html><head><style>.x{color:red}</style></head>"
                b"<body><h1>Hello SkySheep</h1>"
                b"<p>LineA</p><script>evil()</script><p>LineB</p></body></html>"
            )
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/json":
            self._send(200, "application/json", json.dumps({"ok": 1}).encode())
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/html")
            self.end_headers()
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        else:
            self._send(404, "text/plain", b"missing")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def stub_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


async def test_web_fetch_strips_html_and_follows_redirect(stub_server, tmp_path):
    tool = WebFetchTool(allow_private_hosts=True)
    ctx = ToolContext(working_dir=tmp_path)
    out = await tool.run(WebFetchArgs(url=stub_server + "/html"), ctx)
    assert "Hello SkySheep" in out
    assert "LineA" in out and "LineB" in out
    assert "evil()" not in out, "script 内容必须剔除"
    assert "color:red" not in out, "style 内容必须剔除"
    assert out.startswith("[" + stub_server + "/html]")

    out2 = await tool.run(WebFetchArgs(url=stub_server + "/redirect"), ctx)
    assert "Hello SkySheep" in out2, "302 应跟随到 /html"


async def test_web_fetch_errors(stub_server, tmp_path):
    tool = WebFetchTool(allow_private_hosts=True)
    ctx = ToolContext(working_dir=tmp_path)
    out = await tool.run(WebFetchArgs(url=stub_server + "/json"), ctx)
    assert '"ok": 1' in out

    with pytest.raises(ToolError):  # 404 → 报错
        await tool.run(WebFetchArgs(url=stub_server + "/nope"), ctx)
    with pytest.raises(ToolError, match="重定向"):
        await tool.run(WebFetchArgs(url=stub_server + "/loop"), ctx)


async def test_web_fetch_ssrf_guards(tmp_path):
    tool = WebFetchTool()
    ctx = ToolContext(working_dir=tmp_path)
    with pytest.raises(ToolError):
        await tool.run(WebFetchArgs(url="http://127.0.0.1:8000/x"), ctx)
    with pytest.raises(ToolError):
        await tool.run(WebFetchArgs(url="http://192.168.1.10/admin"), ctx)
    with pytest.raises(ToolError):
        await tool.run(WebFetchArgs(url="ftp://example.com/file"), ctx)
    with pytest.raises(ToolError):
        await tool.run(WebFetchArgs(url="http://localhost:11434/"), ctx)


def test_html_to_text_basic():
    html = "<html><head><title>T</title></head><body><h1>A</h1><p>B&amp;C</p></body></html>"
    text = html_to_text(html)
    assert "A" in text and "B&C" in text
    assert "<" not in text


# ---- 检查点 / 回滚 ----


async def test_checkpoint_recorder_and_restore(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1", encoding="utf-8")

    rec = ChangeRecorder()
    registry = ToolRegistry(default_tools(recorder=rec))
    ctx = ToolContext(working_dir=tmp_path)
    await registry.get("edit_file").run(
        EditFileArgs(path="a.txt", old_string="v1", new_string="v2"), ctx
    )
    assert f.read_text(encoding="utf-8") == "v2"
    assert rec.pre == {str(f): b"v1"}

    new_file = tmp_path / "sub" / "new.txt"
    await registry.get("write_file").run(
        WriteFileArgs(path="sub/new.txt", content="n"), ctx
    )
    assert rec.pre[str(new_file)] is None, "新建文件改前不存在 → 记 None"

    store = CheckpointStore()
    assert store.save("s1", {}) is None
    cp = store.save("s1", dict(rec.pre))
    assert sorted(cp["paths"]) == sorted([str(f), str(new_file)])
    assert store.list_for("s1")[0]["id"] == cp["id"]
    assert store.list_for("other") == []

    # 改动继续发生后再回滚：快照之后文件又被改过（等价于并行任务写了同一文件）
    # → 引擎先报冲突不动磁盘；用户确认后带 force 恢复，回滚才落盘
    from skysheep.core.checkpoints import CheckpointConflictError

    f.write_text("v3", encoding="utf-8")
    new_file.write_text("changed", encoding="utf-8")
    with pytest.raises(CheckpointConflictError) as ei:
        store.restore(cp["id"])
    assert sorted(ei.value.conflicts) == sorted([str(f), str(new_file)])
    assert f.read_text(encoding="utf-8") == "v3", "冲突时磁盘未被触碰"
    files = store.restore(cp["id"], force=True)
    assert f.read_text(encoding="utf-8") == "v1"
    assert not new_file.exists(), "新建文件回滚 = 删除"
    assert sorted(files) == sorted(cp["paths"])

    with pytest.raises(KeyError):
        store.restore("cp999")


async def test_write_without_recorder_still_works(tmp_path):
    registry = ToolRegistry(default_tools())  # CLI 场景：不传 recorder
    ctx = ToolContext(working_dir=tmp_path)
    out = await registry.get("write_file").run(WriteFileArgs(path="x.txt", content="hi"), ctx)
    assert "x.txt" in out


# ---- WS 层：消息排队 / 检查点恢复 / status / compact / fs.files / 搜索 ----


def test_message_queue_runs_after_current_turn(home):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "q1.txt", "content": "one"})],
            [TextBlock(text="first done")],
            [TextBlock(text="second done")],
        ]
    )
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "第一条：写文件"}})
        perm = None
        for _ in range(200):
            fr = ws.receive_json()
            if "event" in fr and fr["event"] == "permission_request":
                perm = fr["data"]["request_id"]
                break
        assert perm, "第一轮应停在权限确认上"

        # Agent 未结束时再发一条 → 排队，不报错
        ws.send_json({"id": "c2", "method": "chat.send", "params": {"text": "第二条消息"}})
        ws.send_json(
            {
                "id": "p1",
                "method": "permission.respond",
                "params": {"request_id": perm, "decision": "allow_once"},
            }
        )

        frames, ids_seen = [], set()
        while not {"c1", "c2"} <= ids_seen:
            fr = ws.receive_json()
            frames.append(fr)
            if "id" in fr:
                assert fr.get("ok"), fr
                ids_seen.add(fr["id"])

        order = [fr["id"] for fr in frames if fr.get("id") in ("c1", "c2") and fr.get("ok")]
        assert order == ["c1", "c2"], "排队消息必须等当前轮结束后才执行"
        queue_events = [fr for fr in frames if fr.get("event") == "queue_updated"]
        assert queue_events, "应有 queue_updated 事件"
        assert (home / "proj" / "q1.txt").read_text(encoding="utf-8") == "one"


def test_checkpoint_restore_via_ws(home):
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "cp.txt", "content": "v1"})],
            [TextBlock(text="written")],
        ]
    )
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写检查点文件"}})
        # write_file 需要权限确认：见到请求就放行，直到 c1 完成
        frame = None
        while frame is None:
            fr = ws.receive_json()
            if "event" in fr:
                if fr["event"] == "permission_request":
                    ws.send_json(
                        {
                            "id": "pa",
                            "method": "permission.respond",
                            "params": {"request_id": fr["data"]["request_id"], "decision": "allow_once"},
                        }
                    )
                continue
            if fr.get("id") == "c1":
                frame = fr
        assert frame["ok"]
        cp = frame["result"].get("checkpoint")
        assert cp and cp["paths"] == [str(home / "proj" / "cp.txt")]
        assert (home / "proj" / "cp.txt").read_text(encoding="utf-8") == "v1"

        ws.send_json({"id": "rs", "method": "checkpoint.restore", "params": {"id": cp["id"]}})
        resp = recv_until(ws, "rs")
        assert resp["ok"] and resp["result"]["files"]
        assert not (home / "proj" / "cp.txt").exists(), "新建文件的回滚是删除"

        ws.send_json({"id": "cl", "method": "checkpoint.list", "params": {}})
        listing = recv_until(ws, "cl")
        assert listing["ok"]
        ids = [c["id"] for c in listing["result"]["checkpoints"]]
        assert cp["id"] in ids, "恢复后检查点仍保留在列表里（可再次回滚）"


def test_chat_status_and_fs_files(home):
    (home / "proj" / "hello.py").write_text("print('x')", encoding="utf-8")
    (home / "proj" / ".venv" / "junk.js").parent.mkdir()
    (home / "proj" / ".venv" / "junk.js").write_text("x", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "chat.status", "params": {}})
        st = recv_until(ws, "s1")["result"]
        assert st["context_limit"] > 0 and st["tool_count"] >= 9
        assert st["provider"] == "fake"

        ws.send_json({"id": "f1", "method": "fs.files", "params": {}})
        files = recv_until(ws, "f1")["result"]
        assert "hello.py" in files["files"]
        assert not any(".venv" in p for p in files["files"]), "依赖目录必须跳过"


def test_compact_now_via_ws(home):
    provider = FakeProvider([[TextBlock(text=f"回复{i}")] for i in range(5)])
    provider.with_default([TextBlock(text="摘要：此前完成了大量工作")])
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        for i in range(5):
            ws.send_json({"id": f"c{i}", "method": "chat.send", "params": {"text": f"第{i}轮"}})
            assert recv_until(ws, f"c{i}")["ok"]
        ws.send_json({"id": "cm", "method": "chat.compact", "params": {}})
        r = recv_until(ws, "cm")["result"]
        assert r["compacted"] and r["after"] < r["before"]
        assert r["context_tokens"] > 0


def test_app_notify_via_ws(home, monkeypatch):
    """app.notify：toast 在独立线程发出；ui.json 开关关闭后短路。"""
    calls = []
    monkeypatch.setattr(
        "skysheep.server.backend.ServerBackend._toast_blocking",
        staticmethod(lambda title, body: calls.append((title, body))),
    )
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "app.notify",
                      "params": {"title": "任务完成", "body": "看看结果"}})
        r = recv_until(ws, "n1")
        assert r["ok"] and r["result"]["sent"] is True

        ws.send_json({"id": "u1", "method": "ui.save", "params": {"prefs": {"notify": 0}}})
        recv_until(ws, "u1")
        ws.send_json({"id": "n2", "method": "app.notify", "params": {"title": "再响", "body": "x"}})
        r2 = recv_until(ws, "n2")
        assert r2["result"]["sent"] is False and r2["result"]["reason"] == "disabled"

    assert calls == [("任务完成", "看看结果")]


def test_session_search_via_ws(home):
    with make_client(home, [[TextBlock(text="今天讨论量子纠缠的性质")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "聊聊量子纠缠"}})
        done = recv_until(ws, "c1")
        assert done["ok"]
        sid = done["result"]["session_id"]

        ws.send_json({"id": "q1", "method": "session.search", "params": {"query": "量子"}})
        r = recv_until(ws, "q1")["result"]
        assert len(r["results"]) == 1
        assert r["results"][0]["session_id"] == sid
        assert "量子" in r["results"][0]["snippet"]

        # 会话列表带摘要：最近一轮助手回答的开头
        ws.send_json({"id": "sl", "method": "session.list", "params": {}})
        lst = recv_until(ws, "sl")["result"]
        assert lst["sessions"][0]["summary"].startswith("今天讨论量子纠缠"), "摘要 = 最后一条助手消息开头"

        ws.send_json({"id": "q2", "method": "session.search", "params": {"query": "不存在的词"}})
        assert recv_until(ws, "q2")["result"]["results"] == []
