"""真实 socket 上的协作模式（团队工单全流转 / 对抗四角色流水线）端到端测试。

test_team*.py / test_adversarial.py 的协议级用例走 TestClient（进程内 ASGI 直调），
test_ws_e2e.py 的团队用例覆盖到「轮次耗尽强制交付」与「team.room 空板探针」；
本文件补两条关键路径的真实 uvicorn + websockets socket 版（test_ws_e2e.py 同款
纪律：随机端口真连、真实握手与分帧）：

- 团队：建队广播 → 工单全生命周期（登记 → 派工 → 待验收 → 完成）→ 成员唤醒
  应答 → team.room 探针读到全完成板面 → team.deliver 交付（done 终态 +
  《交付说明》，TeamFinished 经同一连接到达）→ team.log 全量回放 → 会话恢复
  普通回合；
- 对抗：chat.send 带 adversarial 标志 → 发现→调查→建议→裁判四阶段事件 →
  报告作为 assistant 消息带 adversarial 元数据落库（session.resume 刷新回放
  卡片仍在、usage_log 按角色逐行入账）→ 与圆桌/团队同轮互斥的参数错分支。

用 FakeProvider 脚本回放（脚本顺序 = provider_factory 调用顺序：主模型在最前，
队员/角色按 members 顺序跟进），零外部网络；SKYSHEEP_HOME 经 conftest 的
home fixture 隔离到 tmp_path。文件级 e2e 标记：``pytest -m e2e`` 单独跑、
``pytest -m "not e2e"`` 跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sqlite3

import pytest
import websockets
from test_ws_e2e import _rpc

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home, pool):
    """起一个真实 uvicorn 服务，provider_factory 依次弹出 pool 里的 Provider 实例。

    调用顺序即主模型（boot）→ 建队/角色成员构建（members 声明顺序），
    与 test_ws_e2e.py 的 AI 总管用例同一姿势；pool 取空后回退空脚本实例。
    """
    import uvicorn

    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool else FakeProvider([]),
    )
    port = _free_port()
    # 优雅关闭要有上限：Windows Proactor 偶发漏掉一条连接的 connection_lost 时
    # wait_closed() 会无限等，下面 10s 的取消会跳过 lifespan shutdown、漏掉
    # aiosqlite 非 daemon 工作线程，pytest 打完 passed 后永不退出（同 test_ws_e2e）。
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("uvicorn 未能在超时内启动")
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


async def test_real_socket_team_board_lifecycle_deliver_and_room(home):
    """真实 socket 上跑通团队工单板全生命周期到「交付」正常终态：

    建队广播 → 登记 → 派工（in_progress + director assign 频道消息）→ 成员唤醒
    应答 → 待验收 → 完成 → team.room 读到全完成板面 → team.deliver（done 终态，
    TeamFinished 经同一连接到达）→ team.log 全量回放 → 会话恢复普通回合。
    交付与轮次耗尽是两条不同的终态路径（后者已有 test_ws_e2e 覆盖）；工单板
    状态机（pending→in_progress→review→done）、team.room 的 stats 口径与
    team.log 的落库行任何一处接线错误都会在真实分帧上挂。"""
    (home / "home").mkdir(parents=True, exist_ok=True)
    (home / "home" / "config.toml").write_text("[team]\nmax_rounds = 6\n", encoding="utf-8")
    pool = [
        FakeProvider([[TextBlock(text="交付后的普通回答")]]),  # 主模型（交付后恢复普通回合）
        FakeProvider([[TextBlock(text="调研完成：结论 A")]]),  # 队员小研
    ]
    async with live_server(home, pool) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot

            # ---- 建队：广播消息进频道（不唤醒成员）----
            ev1: list = []
            r1 = await _rpc(ws, "t1", "chat.send", {
                "text": "大家好，目标是整理调研结论", "team": True,
                "members": [{"provider": "fa", "model": "ma", "name": "小研",
                             "persona": "研究员"}],
            }, events=ev1)
            assert r1.get("ok") is True, r1
            started = next(e for e in ev1 if e["event"] == "team_started")
            assert started["data"]["director_mode"] == "user"
            assert started["data"]["roster"][0]["name"] == "小研"
            assert started["data"]["roster"][0]["provider"] == "fa"
            ruling = next(e for e in ev1 if e["event"] == "team_message"
                          and e["data"]["from_member"] == "user")
            assert ruling["data"]["to_member"] == "all"
            assert ruling["data"]["msg_kind"] == "ruling"
            assert r1["result"]["team"]["mode"] == "team"
            sid = r1["result"]["session_id"]
            team_id = r1["result"]["team"]["team"]["team_id"]

            # ---- 工单登记 → 派工（in_progress 代发 director assign 消息）----
            k1 = await _rpc(ws, "k1", "team.task_add", {
                "session_id": sid, "title": "整理调研结论",
                "assignee": "小研", "type": "exec",
            })
            assert k1.get("ok") is True, k1
            tid = k1["result"]["task"]["id"]
            assert k1["result"]["task"]["status"] == "pending"

            ev2: list = []
            k2 = await _rpc(ws, "k2", "team.task_update", {
                "session_id": sid, "task_id": tid, "status": "in_progress",
            }, events=ev2)
            assert k2.get("ok") is True, k2
            assert k2["result"]["task"]["status"] == "in_progress"
            assign = k2["result"]["assign_message"]
            assert assign["from"] == "director" and assign["msg_kind"] == "assign"
            assert assign["task_ref"] == tid
            assert any(e["event"] == "team_task_updated"
                       and e["data"]["status"] == "in_progress" for e in ev2)

            # ---- 成员唤醒应答（report 定稿 + 流式增量经真实分帧到达）----
            ev3: list = []
            r2 = await _rpc(ws, "t2", "chat.send", {
                "text": "@小研 干活", "session_id": sid,
            }, events=ev3)
            assert r2.get("ok") is True, r2
            assert r2["result"]["team"]["mode"] == "team"
            assert r2["result"]["team"]["team"]["status"] == "active"
            assert r2["result"]["team"]["team"]["rounds_used"] == 1
            deltas = "".join(e["data"]["text"] for e in ev3
                             if e["event"] == "team_message_delta")
            assert "调研完成" in deltas
            report = next(e for e in ev3 if e["event"] == "team_message"
                          and e["data"]["from_member"] == "小研")
            assert report["data"]["to_member"] == "director"
            assert report["data"]["msg_kind"] == "report"

            # ---- 待验收 → 完成（板只由用户变更：状态机逐格流转）----
            ev4: list = []
            k3 = await _rpc(ws, "k3", "team.task_update", {
                "session_id": sid, "task_id": tid, "status": "review",
            }, events=ev4)
            assert k3.get("ok") is True, k3
            assert k3["result"]["task"]["status"] == "review"
            assert any(e["event"] == "team_task_updated"
                       and e["data"]["status"] == "review" for e in ev4)
            ev5: list = []
            k4 = await _rpc(ws, "k4", "team.task_update", {
                "session_id": sid, "task_id": tid, "status": "done",
            }, events=ev5)
            assert k4.get("ok") is True, k4
            assert k4["result"]["task"]["status"] == "done"
            assert any(e["event"] == "team_task_updated"
                       and e["data"]["status"] == "done" for e in ev5)

            # ---- team.room 探针：读到全完成板面（stats 按状态计数 + 成员用量）----
            g1 = await _rpc(ws, "g1", "team.room", {"session_id": sid})
            assert g1.get("ok") is True, g1
            got = g1["result"]
            assert set(got) == {"active", "session_id", "team", "latest_seq", "stats"}
            assert got["active"] is True and got["session_id"] == sid
            assert got["team"]["team_id"] == team_id
            assert got["team"]["status"] == "active"
            assert got["latest_seq"] > 0
            assert got["stats"]["tasks_by_status"] == {
                "pending": 0, "in_progress": 0, "review": 0, "done": 1, "error": 0}
            assert got["stats"]["member_usage"]["小研"] == {
                "input_tokens": 11, "output_tokens": 7}

            # ---- 交付：用户确认的正常终态（TeamFinished(done) 经同一连接到达）----
            await ws.send(json.dumps({"id": "d1", "method": "team.deliver",
                                      "params": {"session_id": sid}}))
            d1 = None
            finished_ev = None
            while d1 is None or finished_ev is None:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "event" in frame:
                    if frame["event"] == "team_finished":
                        finished_ev = frame
                    continue
                if frame.get("id") == "d1":
                    d1 = frame
            assert d1.get("ok") is True, d1
            assert d1["result"]["status"] == "done"
            assert "《交付说明》" in d1["result"]["summary"]
            assert "整理调研结论" in d1["result"]["summary"]
            assert d1["result"]["team"]["status"] == "done"
            assert finished_ev["data"]["status"] == "done"
            assert "《交付说明》" in finished_ev["data"]["summary"]

            # ---- 交付后：聚合探针回空态；频道全量凭 team_id 走 team.log 回放 ----
            g2 = await _rpc(ws, "g2", "team.room", {"session_id": sid})
            assert g2.get("ok") is True, g2
            assert g2["result"]["active"] is False and g2["result"]["team"] is None
            g3 = await _rpc(ws, "g3", "team.log", {"team_id": team_id})
            assert g3.get("ok") is True, g3
            rows = g3["result"]["messages"]
            seqs = [row["seq"] for row in rows]
            assert seqs == sorted(seqs) and len(seqs) >= 3
            assert rows[0]["from_member"] == "user" and rows[0]["msg_kind"] == "ruling"
            assert any(r["from_member"] == "director" and r["msg_kind"] == "assign"
                       for r in rows)
            assert any(r["from_member"] == "小研" and r["msg_kind"] == "report"
                       for r in rows)

            # ---- 交付后恢复普通回合：消息不再进团队频道 ----
            ev6: list = []
            r3 = await _rpc(ws, "t3", "chat.send", {"text": "谢谢大家"}, events=ev6)
            assert r3.get("ok") is True, r3
            assert r3["result"]["team"] is None
            assert not any(e["event"].startswith("team_") for e in ev6)
            assert "".join(e["data"].get("text", "") for e in ev6
                           if e["event"] == "text_delta") == "交付后的普通回答"


JUDGE_REPORT = (
    "## 对抗审查报告\n\n### 1. a.py:10 空列表越界\n\n"
    "修复：先判空再取元素。\n\n总体：一处高危需立即处理。"
)

FINDER_JSON = json.dumps({
    "findings": [
        {"category": "bug", "severity": "high", "location": "a.py:10",
         "description": "空列表调用 pop 会越界", "evidence": "第 10 行没有判空"},
        {"category": "performance", "severity": "low", "location": "c.py:1",
         "description": "循环里重复编译正则", "evidence": "每次迭代都 re.compile"},
    ]
}, ensure_ascii=False)

INVESTIGATOR_JSON = json.dumps({
    "verdicts": [
        {"id": "F-001", "verdict": "confirmed", "reason": "确实没有判空"},
        {"id": "F-002", "verdict": "refuted", "reason": "正则编译结果被外层 lru_cache 复用"},
    ]
}, ensure_ascii=False)

ADVISOR_JSON = json.dumps({
    "solutions": [
        {"id": "F-001", "fix": "先判空再取元素，或用 next(iter, default)", "risk": "低", "priority": "P0"},
    ]
}, ensure_ascii=False)


async def test_real_socket_adversarial_pipeline_replay_and_mutex(home):
    """真实 socket 上跑通对抗四角色流水线到落库回放：

    chat.send 带 adversarial 标志 → 发现→调查→建议→裁判四阶段事件经真实分帧
    到达 → 报告作为本轮 assistant 消息带 adversarial 元数据落库（session.resume
    刷新回放卡片仍在、usage_log 按角色逐行入账）→ adversarial 与 roundtable /
    team 同发按「不能同时开启」参数错拒绝（连接不断）。"""
    pool = [
        FakeProvider([[TextBlock(text=JUDGE_REPORT)]]),       # 裁判（当前主模型）
        FakeProvider([[TextBlock(text=FINDER_JSON)]]),        # 发现者
        FakeProvider([[TextBlock(text=INVESTIGATOR_JSON)]]),  # 调查者
        FakeProvider([[TextBlock(text=ADVISOR_JSON)]]),       # 建议者
    ]
    async with live_server(home, pool) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot

            ev1: list = []
            r1 = await _rpc(ws, "a1", "chat.send", {
                "text": "审查这段代码：print(pop([]))",
                "adversarial": True,
                "members": [
                    {"provider": "fa", "model": "ma"},
                    {"provider": "fb", "model": "mb"},
                    {"provider": "fc", "model": "mc"},
                ],
            }, events=ev1)
            assert r1.get("ok") is True, r1

            kinds = [e["event"] for e in ev1 if e["event"] != "task_estimate"]
            assert kinds[0] == "turn_started"
            assert "adversarial_started" in kinds
            assert kinds.count("adversarial_finding_proposed") == 2
            assert kinds.count("adversarial_verdict") == 2
            assert "text_delta" in kinds
            assert "adversarial_finished" in kinds

            # 角色名册：发现/调查/建议按 members 顺序，裁判是当前主模型
            started = next(e for e in ev1 if e["event"] == "adversarial_started")
            assert [r["role"] for r in started["data"]["roles"]] == [
                "finder", "investigator", "advisor", "judge"]
            roles = {r["role"]: r for r in started["data"]["roles"]}
            assert roles["judge"]["provider"] == "fake"
            assert roles["finder"]["provider"] == "fa"
            assert roles["investigator"]["provider"] == "fb"
            assert roles["advisor"]["provider"] == "fc"

            # 四阶段流水线（发现者 → 调查者 → 建议者 → 裁判）逐段推进
            phases = [e["data"]["phase"] for e in ev1
                      if e["event"] == "adversarial_phase"]
            assert phases == ["finder", "investigator", "advisor", "judge"]
            verdicts = {e["data"]["finding_id"]: e["data"]["verdict"]
                        for e in ev1 if e["event"] == "adversarial_verdict"}
            assert verdicts == {"F-001": "confirmed", "F-002": "refuted"}

            # 裁判报告流式进主对话，拼回来必须完整
            report = "".join(e["data"].get("text", "") for e in ev1
                             if e["event"] == "text_delta")
            assert report == JUDGE_REPORT

            # 报告 = 本轮正式助手消息，带对抗元数据（裁决统计 + 问题清单）
            assistant = next(e for e in ev1 if e["event"] == "assistant_message")
            msg = assistant["data"]["message"]
            assert msg["content"][0]["text"] == JUDGE_REPORT
            meta = msg["adversarial"]
            assert meta["mode"] == "adversarial"
            assert meta["status"] == "done"
            assert meta["stats"] == {"total": 2, "confirmed": 1, "refuted": 1,
                                     "partial": 0, "pending": 0}
            assert len(meta["findings"]) == 2
            assert meta["findings"][0]["verdict"] == "confirmed"
            assert r1["result"]["adversarial"] == meta
            assert r1["result"]["adversarial_seq"] > 0
            finished = next(e for e in ev1 if e["event"] == "adversarial_finished")
            assert finished["data"]["status"] == "done"
            assert "成立 1" in finished["data"]["summary"]
            assert "推翻 1" in finished["data"]["summary"]
            sid = r1["result"]["session_id"]

            # ---- 刷新回放：session.resume 重发全量历史，对抗卡凭 adversarial 键重建 ----
            r2 = await _rpc(ws, "r1", "session.resume", {"id": sid})
            assert r2.get("ok") is True, r2
            replay = [m for m in r2["result"]["messages"] if m["role"] == "assistant"][-1]
            assert replay["adversarial"] == meta
            assert replay["adversarial"]["findings"]

            # ---- 同轮互斥：adversarial 与 roundtable / team 同发按参数错拒绝 ----
            m1 = await _rpc(ws, "m1", "chat.send", {
                "text": "hi", "adversarial": True, "roundtable": True})
            assert m1.get("ok") is False, m1
            assert "不能同时开启" in m1.get("error", "")
            m2 = await _rpc(ws, "m2", "chat.send", {
                "text": "hi", "adversarial": True, "team": True})
            assert m2.get("ok") is False, m2
            assert "不能同时开启" in m2.get("error", "")
            # 拒绝分支不断连：连接照常可用
            boot2 = await _rpc(ws, "b2", "boot")
            assert boot2.get("ok") is True, boot2

    # 落库：user + assistant（带 adversarial 键）两行；四个角色按序逐行入账
    db = sqlite3.connect(home / "home" / "skysheep.db")
    try:
        rows = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
        usage = db.execute("SELECT provider FROM usage_log ORDER BY id").fetchall()
    finally:
        db.close()
    assert [r[0] for r in rows] == ["user", "assistant"]
    saved = json.loads(rows[1][1])
    assert saved["adversarial"] == meta
    assert [u[0] for u in usage] == ["fa", "fb", "fc", "fake"]
