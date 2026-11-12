"""失败恢复落地：子代理一键重跑、未决权限卡恢复字段、崩溃残留清扫。

- runs.retry 对 subagent 运行按原 agent_type/prompt 真正重新派生（不再把
  prompt 当普通消息回传），返回 kind=subagent + 新 task_id；
- permission.queue 返回与 permission_request 事件同构的完整字段
  （origin/scope/reversible/revert_note/diff/note/rule_kind/rule_pattern），
  前端断线重连/刷新后可重建确认卡；
- browser_cdp.sweep_orphan_browser_profiles 清理崩溃残留的无头浏览器临时目录，
  进程还活着的目录跳过。
"""

from __future__ import annotations

import asyncio
import sqlite3

from fastapi.testclient import TestClient

from skysheep.core.runs import INTERRUPTED, RUNNING_STATES, RunCenter
from skysheep.messages import TextBlock
from skysheep.models.base import ProviderDone, ProviderTextDelta
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app
from skysheep.tools import browser_cdp


def make_client(home, script, provider=None):
    provider = provider or FakeProvider(script)
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    return TestClient(app)


def recv_until(ws, wanted_id=None):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


def _rpc(ws, mid, method, params=None):
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    return recv_until(ws, mid)


def _run_states(ws, mid="rl"):
    return _rpc(ws, mid, "runs.list", {"limit": 50})["result"]["runs"]


# ---- 0. 运行终态与身份（本轮修复的回归） ----
#
# 旧实现三个实际缺陷：模型失败被收口成 completed；turn-<n> 跨重启覆盖旧记录；
# 重发接口不看状态、已完成的运行也能重发。以下用例各自钉住一条。


class _BrokenProvider(FakeProvider):
    """模型调用必失败（模拟 401）：验证失败轮不会被记成 completed。"""

    async def stream(self, messages, tool_schemas, effort=None):
        raise RuntimeError("401 unauthorized")
        yield  # noqa: B012 - 保持异步生成器语义


def test_model_failure_marks_run_failed(home):
    """Provider 抛错：运行中心终态必须是 failed，不能是 completed。

    Agent 会把 Provider 异常吞成 ErrorEvent 后正常收尾，旧收口只看外层
    Python 异常，于是把失败轮写成 completed（错误摘要还在但状态是成功）。
    """
    with make_client(home, [], provider=_BrokenProvider([])) as client, \
            client.websocket_connect("/ws") as ws:
        r = _rpc(ws, "c1", "chat.send", {"text": "会失败的一轮"})
        assert r["ok"] is True  # 请求被正常处理（错误以事件呈现）
        runs = _run_states(ws)
        turn = [x for x in runs if x["kind"] == "turn"]
        assert turn, runs
        assert turn[0]["state"] == "failed", turn[0]
        assert turn[0]["error"], "失败轮应带错误摘要"


def test_completed_run_is_not_retryable(home):
    """已完成的运行不允许重发：状态约束由后端强制，不能只靠前端隐藏按钮。"""
    with make_client(home, ["好了"]) as client, client.websocket_connect("/ws") as ws:
        _rpc(ws, "c1", "chat.send", {"text": "正常一轮"})
        turn = [x for x in _run_states(ws) if x["kind"] == "turn"][0]
        assert turn["state"] == "completed", turn
        r = _rpc(ws, "r1", "runs.retry", {"run_id": turn["id"]})
        assert r["ok"] is False, r
        assert "不可重发" in r["error"]


def test_run_id_survives_restart_without_collision(home):
    """运行 id 跨重启不重复：同 home 重开不会覆盖旧记录，列表无重复 id。"""
    path = home / "runs.json"
    rc = RunCenter(path)
    first = rc.allocate_id("turn")
    rc.register(first, "turn", "A", title="旧运行", retry_text="旧任务")
    rc.update(first, state="completed")

    rc2 = RunCenter(path)  # 等价应用重启（重新装载快照）
    second = rc2.allocate_id("turn")
    assert second != first, "重启后新 id 不能与旧 id 相同"
    rc2.register(second, "turn", "B", title="新运行", retry_text="新任务")
    ids = [x["id"] for x in rc2.list_runs()]
    assert len(ids) == len(set(ids)), ids
    assert rc2.get(first).session_id == "A", "旧记录不得被覆盖"


def test_register_same_id_does_not_duplicate_order(tmp_path):
    """同 id 重复登记：内容更新但不产生幽灵条目（列表里只有一个）。"""
    rc = RunCenter(tmp_path / "runs.json")
    rc.register("r1", "turn", "s1", retry_text="a")
    rc.register("r1", "turn", "s1", retry_text="b")
    rows = rc.list_runs(limit=50)
    assert len(rows) == 1, rows
    assert rows[0]["retry_text"] == "b"


def test_retryable_run_requires_failed_or_interrupted(tmp_path):
    """只有失败/中断且带原文的运行可重发（状态约束的单元级证据）。"""
    rc = RunCenter(tmp_path / "runs.json")
    rc.register("done", "turn", "s", retry_text="x")
    rc.update("done", state="completed")
    assert rc.retryable_run("done") is None
    rc.register("bad", "turn", "s", retry_text="x")
    rc.update("bad", state="failed")
    assert rc.retryable_run("bad") is not None


# ---- 0b. 隔离子代理登记时序 ----


def test_isolated_run_registered_with_isolated_flag(tmp_path):
    """隔离子代理的运行记录必须带 isolated=true（重跑依赖它保持隔离语义）。

    旧实现先 _new_record 登记 Run、后设 rec.isolated，导致运行记录里是 False，
    runs.retry 重跑时会把隔离任务降级成普通任务。
    """
    from skysheep.core.runs import RunCenter as _RC
    from skysheep.core.subagent import TaskManager

    rc = _RC(tmp_path / "runs.json")
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="x")]]),
        working_dir=tmp_path,
    )
    tasks.run_center = rc
    rec = tasks._new_record("task", "隔离活", session_id="s1", isolated=True)
    run = rc.get("subagent-" + rec.id)
    assert run is not None, "子代理运行应已登记"
    assert run.isolated is True, "运行记录必须带隔离标志"
    assert rec.isolated is True and rec.branch == f"skysheep/task-{rec.id}"


# ---- 1. 子代理失败/中断后一键重跑 ----
#
# runs.retry 对 subagent 分支应真正重新派生同型任务，而不是把 prompt 回传
# 前端当普通消息发。需要 Run 记录带 agent_type / isolated，重跑语义在 WS
# 契约层验证：返回 kind=subagent 与新 task_id，任务簿里出现新任务。


def test_run_persists_subagent_meta(home):
    """Run 记录持久化 agent_type / isolated，重载后仍在（重跑依赖的最小信息）。"""
    path = home / "runs.json"
    rc = RunCenter(path)
    rc.register(
        "subagent-abc", "subagent", "s1",
        title="task：写个报告", state="queued", retry_text="写个报告",
        agent_type="task", isolated=False,
    )
    rc.update("subagent-abc", state=INTERRUPTED, error="应用重启")
    rc.save()

    rc2 = RunCenter(path)
    r = rc2.get("subagent-abc")
    assert r is not None
    assert r.kind == "subagent"
    assert r.agent_type == "task"
    assert r.isolated is False
    assert r.state == INTERRUPTED
    assert r.retryable is True
    # 中断状态在 RUNNING_STATES 之外，不会再次被改判
    assert r.state not in RUNNING_STATES


def test_run_retry_subagent_reruns_task(home):
    """runs.retry 对子代理运行：按原 agent_type/prompt 重新派生，返回新 task_id。

    演示模式的 TaskManager 直接用 fake provider 跑后台任务——这里只验证 WS
    契约与登记面：返回 kind=subagent，且任务簿出现新记录（running/queued）。
    """
    with make_client(home, ["回答好了"]) as client, client.websocket_connect("/ws") as ws:
        # 造一条「失败/中断且可重跑」的子代理运行：直接通过 runs 登记面
        r = _rpc(ws, "s1", "runs.retry", {"run_id": "no-such"})
        assert r["ok"] is False

        # 引擎内部登记一条 subagent 运行（模拟中断后带重跑信息的记录）
        backend = client.app.state.backend
        rc = backend.runs
        rc.register(
            "subagent-zzz", "subagent", "",
            title="task：重跑我", state="queued", retry_text="重跑我",
            agent_type="task", isolated=False,
        )
        rc.update("subagent-zzz", state=INTERRUPTED, error="应用重启")

        rr = _rpc(ws, "s2", "runs.retry", {"run_id": "subagent-zzz"})
        assert rr["ok"] is True, rr
        result = rr["result"]
        assert result["kind"] == "subagent"
        assert result["task_id"]
        # 前端拿到 task_id 后直接提示重跑成功，不再把 prompt 当普通消息发
        assert "text" not in result or not result.get("text")


# ---- 2. 未决权限队列：返回重建确认卡的完整字段 ----
#
# 断线重连/刷新后前端靠 permission.queue 重建卡：字段必须与
# permission_request 事件同构（origin/scope/reversible/revert_note/diff/
# note/rule_kind/rule_pattern），否则卡片退化成空槽。


async def test_permission_queue_returns_recovery_fields(home):
    """permission.queue 空队列正常；注入未决请求后返回重建字段。"""
    with make_client(home, ["好的"]) as client, client.websocket_connect("/ws") as ws:
        r = _rpc(ws, "p1", "permission.queue")
        assert r["ok"] is True
        assert r["result"]["pending"] == []

        # 直接在 agent._pending 里塞一个未决请求（模拟轮次正在等用户决策）
        backend = client.app.state.backend
        agent = backend._base_agent
        import asyncio

        from skysheep.security.gate import (
            Decision,
            PendingPermission,
            Safety,
        )
        pending = PendingPermission(
            request_id="req-1",
            tool_name="write_file",
            arg_text="write_file path=a.txt content=x",
            safety=Safety.WRITE,
            detail="写入 a.txt",
            diff="--- a/a.txt\n+++ b/a.txt\n+x",
            note="测试 note",
            origin="主会话",
            scope="工作目录内：a.txt",
            reversible=True,
            revert_note="改动已进检查点",
            _future=asyncio.get_running_loop().create_future(),
        )
        pending.resolve(Decision.ALLOW_ALWAYS)  # 只做字段断言，不真等决策
        agent._pending["req-1"] = pending
        try:
            r2 = _rpc(ws, "p2", "permission.queue")
            assert r2["ok"] is True
            items = r2["result"]["pending"]
            assert any(x["request_id"] == "req-1" for x in items), items
            item = next(x for x in items if x["request_id"] == "req-1")
            assert item["tool_name"] == "write_file"
            assert item["origin"] == "主会话"
            assert item["scope"] == "工作目录内：a.txt"
            assert item["reversible"] is True
            assert item["revert_note"]
            assert item["diff"] == "--- a/a.txt\n+++ b/a.txt\n+x"
            assert item["note"] == "测试 note"
            assert item["rule_kind"] == ""
        finally:
            agent._pending.pop("req-1", None)


class _FakeTeamOrch:
    """最小团队编排器替身：只提供 permission.queue 枚举用的 agents()。"""

    def __init__(self, agents):
        self._agents = agents

    def agents(self):
        return list(self._agents)


async def test_permission_queue_scans_team_member_agents(home):
    """团队成员 Agent 的未决请求也进 permission.queue（collab-3）。

    成员 Agent 挂在各会话编排器（不在 runtimes），决策路由已覆盖他们；
    队列枚举不跟上，成员的确认卡刷新/断线后就无法重建——后端轮挂在等
    决策上＝假死。
    """
    with make_client(home, ["好的"]) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend

        from skysheep.security.gate import Decision, PendingPermission, Safety

        pending = PendingPermission(
            request_id="member-req-1",
            tool_name="write_file",
            arg_text="write_file path=b.txt content=x",
            safety=Safety.WRITE,
            detail="成员写入 b.txt",
            _future=asyncio.get_running_loop().create_future(),
        )
        pending.resolve(Decision.ALLOW_ALWAYS)  # 只做字段断言，不真等决策

        class _MemberAgent:
            _pending = {"member-req-1": pending}

        # 成员 Agent 挂在编排器上：按会话 sid 登记进 _teams
        backend._teams["team-sid-1"] = _FakeTeamOrch([_MemberAgent()])
        try:
            r = _rpc(ws, "p3", "permission.queue")
            assert r["ok"] is True
            items = r["result"]["pending"]
            hit = next(x for x in items if x["request_id"] == "member-req-1")
            assert hit["session_id"] == "team-sid-1"
            assert hit["tool_name"] == "write_file"
            assert hit["detail"] == "成员写入 b.txt"
        finally:
            backend._teams.pop("team-sid-1", None)


# ---- 3. 崩溃残留清扫：无头浏览器临时目录 ----
#
# sweep_orphan_browser_profiles 只删进程已死的 skysheep-edge-* 目录；
# 目录被占用（进程还活着）时跳过，绝不误删正在使用的实例。


def test_sweep_orphan_browser_profiles_removes_dead_only(tmp_path):
    tempdir = tmp_path / "temp"
    tempdir.mkdir()
    dead = tempdir / "skysheep-edge-abc"
    dead.mkdir()
    (dead / "Default").mkdir()
    (dead / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    # 无关目录不碰
    other = tempdir / "skysheep-other"
    other.mkdir()

    removed = browser_cdp.sweep_orphan_browser_profiles(str(tempdir))

    assert removed == 1
    assert not dead.exists()
    assert other.exists(), "不得碰无关目录"


def test_sweep_orphan_browser_profiles_skips_locked_dir(tmp_path, monkeypatch):
    """进程还活着的目录（rmtree 抛 OSError）跳过，不影响其它清理。"""
    import shutil as _shutil

    tempdir = tmp_path / "temp"
    tempdir.mkdir()
    live = tempdir / "skysheep-edge-live"
    live.mkdir()
    (live / "lockfile").write_text("locked", encoding="utf-8")
    dead = tempdir / "skysheep-edge-zzz"
    dead.mkdir()
    # live 目录模拟被占用：rmtree 抛错 → 跳过；dead 正常删掉
    real_rmtree = _shutil.rmtree

    def fake_rmtree(path, *a, **kw):
        if str(path).endswith("skysheep-edge-live"):
            raise OSError(32, "another process is using this file")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(_shutil, "rmtree", fake_rmtree)
    removed = browser_cdp.sweep_orphan_browser_profiles(str(tempdir))
    assert removed == 1
    assert live.exists(), "被占用的目录必须跳过"
    assert not dead.exists()


def test_sweep_orphan_browser_profiles_ignores_missing_tempdir(tmp_path):
    """tempdir 不存在（被清理/路径拼错）时安静返回 0，不抛。"""
    assert browser_cdp.sweep_orphan_browser_profiles(str(tmp_path / "nope")) == 0


class _PausingProvider(FakeProvider):
    """吐第一段后停顿：给测试留出「轮还没结束」的观察窗口。"""

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append(list(messages))
        yield ProviderTextDelta("第一段")
        await asyncio.sleep(0.8)
        yield ProviderTextDelta("第二段")
        yield ProviderDone(input_tokens=5, output_tokens=4)


def _db_rows(home, sid=None):
    # home 夹具返回 tmp_path，SKYSHEEP_HOME 指向 tmp_path/home
    conn = sqlite3.connect(str(home / "home" / "skysheep.db"))
    try:
        sql = "SELECT role, content FROM messages"
        args: tuple = ()
        if sid:
            sql += " WHERE session_id = ?"
            args = (sid,)
        sql += " ORDER BY seq"
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_inflight_messages_persist_before_turn_ends(home):
    """轮末批量落库的旧口径在崩溃时整轮丢失：用户消息与已完成助手消息要
    在轮次进行中就已经在库里（崩溃/强退后至少能看到已经发生的部分）。"""
    client = make_client(home, [], provider=_PausingProvider([]))
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send",
                      "params": {"text": "先落库再回答"}})
        # 等到 turn_started：此时用户消息应已在库
        while True:
            frame = ws.receive_json()
            if frame.get("event") == "turn_started":
                break
        rows = _db_rows(home)
        assert any(role == "user" and "先落库再回答" in content for role, content in rows), rows

        # 等 assistant_message：此时助手消息也应已在库（轮次尚未收尾）
        while True:
            frame = ws.receive_json()
            if frame.get("event") == "assistant_message":
                break
        rows = _db_rows(home)
        assistant = [c for r, c in rows if r == "assistant"]
        assert assistant and "第二段" in assistant[-1], rows

        # 收尾：模型脚本已耗尽，等 RPC 回包
        recv_until(ws, "c1")


def test_events_replay_returns_missed_frames(home):
    """断线重连补帧：事件带全局 seq，可按 after_seq 补发；跨轮补不齐时
    返回 gap=True 让前端改走历史重载。"""
    provider = FakeProvider([[TextBlock(text="第一轮回答")], [TextBlock(text="第二轮回答")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        def run_turn(mid, text):
            ws.send_json({"id": mid, "method": "chat.send", "params": {"text": text}})
            frames = []
            while True:
                frame = ws.receive_json()
                if "event" in frame:
                    frames.append(frame)
                    continue
                if frame.get("id") == mid:
                    return frames

        frames1 = run_turn("c1", "第一问")
        seqs = [f["data"]["ev_seq"] for f in frames1 if isinstance(f["data"].get("ev_seq"), int)]
        assert seqs and seqs == sorted(seqs), "事件帧必须带单调递增的 seq"
        sid = next(f["data"]["session_id"] for f in frames1 if f["data"].get("session_id"))

        after = seqs[len(seqs) // 2]
        r = _rpc(ws, "rp", "events.replay",
                 {"session_id": sid, "after_seq": after})["result"]
        got = r["events"]
        assert got and all(e["ev_seq"] > after for e in got)
        assert [e["ev_seq"] for e in got] == sorted(e["ev_seq"] for e in got)
        assert got[-1]["ev_seq"] == seqs[-1]
        assert r["gap"] is False

        # 第二轮会把缓冲重置为「当前轮」：从 0 补会缺口，必须报 gap
        run_turn("c2", "第二问")
        r2 = _rpc(ws, "rp2", "events.replay",
                  {"session_id": sid, "after_seq": 0})["result"]
        assert r2["gap"] is True
        assert r2["events"], "gap 也要把当前轮还能给的帧带回来"
