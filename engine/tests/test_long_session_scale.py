"""长会话规模性能基线（5000 条消息）。

前端已按 HISTORY_SYNC_LIMIT=200 / HISTORY_CHUNK=100 分批渲染；引擎侧的
「几千条会话不再撑爆」承诺要落在三处可测的链路上：

1. store 层：5000 条消息的写入与 load_messages 全量回读在秒级完成；
2. 服务层：5000 条历史进入下一轮 run_turn（真实 Agent 循环）不被持久化
   或历史装配拖挂；
3. 压缩层：超过上下文窗口的 5000 条历史触发自动压缩并成功收敛。

用 ``@pytest.mark.slow`` 标记：日常提交不跑，发版/专项验证时显式
``pytest -m slow`` 执行。规模取 5000（清单定义的「长会话」上限档）。

另有服务层历史窗口化一组（2026-10）：切会话/恢复/分叉/boot 的 messages
只下发尾部窗口（HISTORY_SYNC_LIMIT=200），更早的经 messages.earlier 向前
翻页——大会话打开的 WS 载荷与会话总长脱钩。窗口化用例走真实 WS 协议
（TestClient），造 300+ 条消息会话（快，不吃 5000 条的写入预算）。
"""

from __future__ import annotations

import os
import time

import pytest
from conftest import FakeProvider

from skysheep.messages import Message, TextBlock
from skysheep.session.store import SessionStore

pytestmark = pytest.mark.slow

N_MESSAGES = 5000
# 同一断言本地实测写入 0.7~5.4s，CI（windows runner + coverage 追踪）实测可到 43~52s；
# 预算按环境分档：本地保持紧档守 M16 级写放大回归，CI 档只挡数量级劣化。
_BUDGET_SCALE = 4.0 if os.environ.get("GITHUB_ACTIONS") == "true" else 1.0
BUDGET_WRITE_S = 30.0 * _BUDGET_SCALE
BUDGET_READ_S = 10.0 * _BUDGET_SCALE
# 清单三档：500 / 1000 / 5000。小档在每次 slow 运行中都跑，大档守住上限档。
BULK_SIZES = [500, 1000, 5000]

# ---- 服务层历史窗口化 ----

# 160 轮 × 2 条（user+assistant）= 320 条：越过 HISTORY_SYNC_LIMIT=200，
# 窗口起点 120、「加载更早」还有 120 条可翻（两页）。
TURNS = 160
TOTAL = 2 * TURNS


def _ws_client(home, script_text="回应"):
    """隔离 home 的真实 FastAPI 应用；FakeProvider 默认回复供多轮 chat.send。"""
    from fastapi.testclient import TestClient

    from skysheep.server import create_app

    provider = FakeProvider([]).with_default([TextBlock(text=script_text)])
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    return TestClient(app)


def _rpc(ws, mid, method, params=None):
    """单发单收：跳过事件帧，取回本请求的回复帧。"""
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            continue
        assert frame.get("id") == mid, f"等 {mid} 时收到别的回复：{frame}"
        return frame


def _long_session(ws, turns=TURNS):
    """新建会话并灌 2*turns 条 user/assistant（每轮 user=问题i / assistant=回应）。"""
    sid = _rpc(ws, "n", "session.new")["result"]["id"]
    for i in range(turns):
        r = _rpc(ws, f"c{i}", "chat.send", {"text": f"问题{i}"})
        assert r["ok"], r.get("error")
    return sid


def test_session_open_windows_history_tail(home):
    """大会话打开只下发尾部窗口：resume / peek / boot 同一带 messages_total/offset。

    320 条会话三处回包都只装 200 条 brief，offset=120 指向窗口之前还有多少条；
    窗口内容必须是最后 200 条（首条 = 第 60 问），前端据此决定「加载更早」去留。
    """
    with _ws_client(home) as client, client.websocket_connect("/ws") as ws:
        sid = _long_session(ws)

        r = _rpc(ws, "r", "session.resume", {"id": sid})
        assert r["ok"], r.get("error")
        res = r["result"]
        assert len(res["messages"]) == 200
        assert res["messages_total"] == TOTAL
        assert res["messages_offset"] == TOTAL - 200 == 120
        assert res["messages"][0]["role"] == "user"
        assert res["messages"][0]["text"] == "问题60"
        assert res["messages"][-1]["role"] == "assistant"
        assert res["messages"][-1]["text"] == "回应"

        r = _rpc(ws, "p", "session.peek_messages", {"id": sid})
        assert r["ok"], r.get("error")
        res = r["result"]
        assert len(res["messages"]) == 200
        assert res["messages_total"] == TOTAL
        assert res["messages_offset"] == 120

        r = _rpc(ws, "b", "boot")
        assert r["ok"], r.get("error")
        snap = r["result"]["session"]
        assert snap["id"] == sid
        assert len(snap["messages"]) == 200
        assert snap["messages_total"] == TOTAL
        assert snap["messages_offset"] == 120


def test_messages_earlier_paging_boundaries(home):
    """messages.earlier 翻页边界：缺省份量、limit 截断、翻到底、越过第一条。"""
    with _ws_client(home) as client, client.websocket_connect("/ws") as ws:
        sid = _long_session(ws)
        window = _rpc(ws, "r", "session.resume", {"id": sid})["result"]["messages"]
        first_seq = window[0]["seq"]  # 已加载最旧一条（u/a 第 120 条）

        # 第一页（缺省 limit=HISTORY_CHUNK=100）：窗口之前的最后 100 条，升序衔接
        r = _rpc(ws, "e1", "messages.earlier", {"session_id": sid, "before_seq": first_seq})
        assert r["ok"], r.get("error")
        p1 = r["result"]
        assert len(p1["messages"]) == 100
        assert p1["messages_total"] == TOTAL
        assert p1["messages_offset"] == 20
        assert p1["has_more"] is True
        assert p1["messages"][0]["text"] == "问题10"
        assert p1["messages"][-1]["role"] == "assistant"
        assert p1["messages"][-1]["text"] == "回应"
        assert [m["text"] for m in p1["messages"] if m["role"] == "user"] == [
            f"问题{i}" for i in range(10, 60)
        ]
        seqs = [m["seq"] for m in p1["messages"]]
        assert seqs == sorted(seqs) and seqs[-1] < first_seq

        # limit 截断：同一边界只要 50 条 → 第 70..119 条（问题35 起），仍有更早
        r = _rpc(ws, "e2", "messages.earlier",
                 {"session_id": sid, "before_seq": first_seq, "limit": 50})
        p2 = r["result"]
        assert len(p2["messages"]) == 50
        assert p2["messages"][0]["text"] == "问题35"
        assert p2["messages_offset"] == 70
        assert p2["has_more"] is True

        # 翻到底：第一条之前的窗口边界 → 剩余 20 条全给，has_more=False
        p1_first_seq = p1["messages"][0]["seq"]  # u/a 第 20 条（问题10）
        r = _rpc(ws, "e3", "messages.earlier", {"session_id": sid, "before_seq": p1_first_seq})
        p3 = r["result"]
        assert [m["text"] for m in p3["messages"] if m["role"] == "user"] == [
            f"问题{i}" for i in range(10)
        ]
        assert p3["messages_offset"] == 0
        assert p3["has_more"] is False
        assert p3["messages_total"] == TOTAL

        # 越过第一条再翻：空页、has_more=False（按钮收起的口径）
        first_msg_seq = p3["messages"][0]["seq"]
        r = _rpc(ws, "e4", "messages.earlier", {"session_id": sid, "before_seq": first_msg_seq})
        p4 = r["result"]
        assert p4["messages"] == []
        assert p4["has_more"] is False
        assert p4["messages_offset"] == 0
        assert p4["messages_total"] == TOTAL

        # 防滥用与坏参：超上限 limit 不炸（钳到 HISTORY_EARLIER_MAX 内）；缺
        # before_seq / 未知会话都报错
        r = _rpc(ws, "e5", "messages.earlier",
                 {"session_id": sid, "before_seq": first_seq, "limit": 100000})
        assert r["ok"] and len(r["result"]["messages"]) == 120  # 窗口前全部
        r = _rpc(ws, "e6", "messages.earlier", {"session_id": sid})
        assert not r["ok"] and "before_seq" in r["error"]
        r = _rpc(ws, "e7", "messages.earlier",
                 {"session_id": "no-such-session", "before_seq": 1})
        assert not r["ok"] and "session not found" in r["error"]


def test_messages_earlier_remote_scope(home, monkeypatch):
    """远程越权（B 族口径与 session.peek_messages 一致）：跨项目会话翻页必拒，
    连错误文案都与 peek 相同（不给枚举探测区分信号）；当前项目会话照常翻页。"""
    from skysheep.server import app as server_app

    # 先在本机连接里把会话灌进项目 B（与远程连接的当前项目不同 → 远程不可见）
    proj_b = home / "proj-b"
    proj_b.mkdir(exist_ok=True)
    with _ws_client(home) as client, client.websocket_connect("/ws") as ws:
        sw = _rpc(ws, "sw", "project.switch", {"path": str(proj_b)})
        assert sw["ok"], sw.get("error")
        foreign_sid = _long_session(ws, turns=2)
        # 切回默认项目（等回执再断连，避免会话落在旧项目——test_peek_cross_project 同款竞态）
        sw0 = _rpc(ws, "sw0", "project.switch", {"path": str(home / "proj")})
        assert sw0["ok"], sw0.get("error")

    # 远程来源：整连接按 local=False 对待（与 test_peek_cross_project 同款桩法）
    monkeypatch.setattr(server_app, "_client_is_local", lambda ws: False)
    with _ws_client(home) as client, client.websocket_connect("/ws") as ws:
        # 越权：项目 B 的会话，远程当前项目口径下不可见
        r = _rpc(ws, "d1", "messages.earlier", {"session_id": foreign_sid, "before_seq": 1})
        assert not r["ok"] and "session not found" in r["error"]

        # 放行侧：远程自己建的当前项目会话，翻页照常工作
        sid = _rpc(ws, "n", "session.new")["result"]["id"]
        r = _rpc(ws, "c", "chat.send", {"text": "远程会话"})
        assert r["ok"], r.get("error")
        window = _rpc(ws, "r", "session.resume", {"id": sid})["result"]["messages"]
        assert [m["role"] for m in window] == ["user", "assistant"]
        r = _rpc(ws, "e1", "messages.earlier",
                 {"session_id": sid, "before_seq": window[-1]["seq"]})
        assert r["ok"], r.get("error")
        got = r["result"]
        assert [m["text"] for m in got["messages"]] == ["远程会话"]
        assert got["has_more"] is False and got["messages_offset"] == 0
        assert got["messages_total"] == 2


def _bulk_messages(n: int) -> list[Message]:
    """n 条交替 user/assistant，正文带序号便于断言排序与完整性。"""
    out: list[Message] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(Message(
            role=role,
            content=[TextBlock(text=f"消息 {i}：" + ("问题" if role == "user" else "回答") * 8)],
        ))
    return out


async def _fresh_store(tmp_path) -> SessionStore:
    return await SessionStore(tmp_path / "long.db").connect()


@pytest.mark.parametrize("n", BULK_SIZES)
async def test_store_write_and_read_bulk_messages(tmp_path, n):
    """500 / 1000 / 5000 条消息：写入与全量回读在预算内（本地紧档，CI 档 ×4）、条数与顺序完整。"""
    store = await _fresh_store(tmp_path)
    try:
        sess = await store.create_session(1, title="长会话")
        msgs = _bulk_messages(n)

        t0 = time.monotonic()
        for m in msgs:
            await store.append_message(sess.id, m)
        write_s = time.monotonic() - t0
        assert write_s < BUDGET_WRITE_S, f"{n} 条写入耗时 {write_s:.1f}s 超预算"

        t0 = time.monotonic()
        loaded = await store.load_messages(sess.id)
        read_s = time.monotonic() - t0
        assert read_s < BUDGET_READ_S, f"{n} 条回读耗时 {read_s:.1f}s 超预算"

        assert len(loaded) == n
        assert loaded[0].seq < loaded[-1].seq, "seq 必须随写入顺序单调"
        assert "消息 0：" in loaded[0].text and f"消息 {n - 1}：" in loaded[-1].text
        # seq 连续无空洞
        seqs = [m.seq for m in loaded]
        assert seqs == list(range(seqs[0], seqs[0] + n))
    finally:
        await store.close()


async def test_agent_turn_with_5000_message_history(tmp_path, home):
    """5000 条历史进入真实 Agent 循环：装配 + 下一轮流式在预算内完成。"""
    from skysheep.core.agent import Agent
    from skysheep.security.gate import PermissionGate
    from skysheep.tools import ToolRegistry

    store = await _fresh_store(tmp_path)
    try:
        sess = await store.create_session(1, title="长会话进轮")
        history = _bulk_messages(N_MESSAGES)
        for m in history:
            await store.append_message(sess.id, m)

        provider = FakeProvider([[TextBlock(text="收到，长对话也正常")]])
        agent = Agent(
            provider=provider,
            registry=ToolRegistry([]),
            gate=PermissionGate(store=None, project_id=None),
            working_dir=home / "proj",
        )
        agent.set_system("system")

        t0 = time.monotonic()
        loaded = await store.load_messages(sess.id)
        agent.load_history(loaded)
        load_s = time.monotonic() - t0

        t0 = time.monotonic()
        events = []
        async for ev in agent.run_turn("再问一句"):
            events.append(ev.kind)
        turn_s = time.monotonic() - t0

        assert "text_delta" in events and "turn_finished" in events
        assert turn_s < 30.0 * _BUDGET_SCALE, f"5000 条历史下开新一轮耗时 {turn_s:.1f}s 超预算"
        assert load_s < BUDGET_READ_S, f"5000 条装配耗时 {load_s:.1f}s 超预算"
        # 轮后历史 = 旧 5000 + 本轮 user/assistant
        assert len(agent.history) >= N_MESSAGES + 2
    finally:
        await store.close()
