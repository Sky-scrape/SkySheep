"""E 组 · 自动化与后台验收：定时任务、编排、子代理、工厂。

都走真实引擎装配（cron.run_now / pipeline.start / spawn_agent），
模型仍由 FakeProvider 脚本回放，断言落盘、依赖顺序与终态。
"""

from __future__ import annotations

import time

import pytest
from _helpers import make_client, make_shared_client, rpc

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import app as server_app

pytestmark = pytest.mark.acceptance


def _wait_pipeline(ws, pipe_id: str, wanted=("done", "failed"), timeout: float = 20.0):
    end = time.time() + timeout
    last = None
    i = 0
    while time.time() < end:
        i += 1
        frame = rpc(ws, f"w{i}", "pipeline.get", {"id": pipe_id})
        if frame.get("ok"):
            last = frame["result"]["pipeline"]
            if last["status"] in wanted:
                return last
        time.sleep(0.05)
    raise AssertionError(f"流水线未在 {timeout}s 内进入 {wanted}：{last}")


def test_e1_cron_lifecycle_and_unattended_gate(home):
    """E1 定时任务：建/列/改/停/删；未预授权写被拒，预授权后放行。"""
    script = [
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="结果一")],
        [ToolUseBlock(id="w2", name="write_file",
                      input={"path": "ok.txt", "content": "y"})],
        [TextBlock(text="结果二")],
    ]
    with make_shared_client(home, script) as client, client.websocket_connect("/ws") as ws:
        task = rpc(ws, "ca", "cron.add", {
            "name": "每日报告", "prompt": "写个文件",
            "schedule_type": "daily", "time_of_day": "09:00",
            "allowed_tools": [],
        })["result"]
        assert task["enabled"] is True and task["next_run_at"] > 0

        listed = rpc(ws, "cl", "cron.list")["result"]["tasks"]
        assert len(listed) == 1 and listed[0]["id"] == task["id"]
        assert rpc(ws, "bad", "cron.add", {
            "name": "x", "prompt": "y", "schedule_type": "monthly",
        })["ok"] is False

        # 未预授权：写被无人值守门控拒绝，但任务照常收尾
        assert rpc(ws, "r1", "cron.run_now", {"id": task["id"]})["ok"]
        assert not (home / "proj" / "secret.txt").exists()
        row = rpc(ws, "g1", "cron.list")["result"]["tasks"][0]
        assert row["last_status"] == "ok" and row["last_result"] == "结果一"

        # 预授权 write_file 后放行
        rpc(ws, "cu", "cron.update", {"id": task["id"], "allowed_tools": ["write_file"]})
        assert rpc(ws, "r2", "cron.run_now", {"id": task["id"]})["ok"]
        assert (home / "proj" / "ok.txt").read_text(encoding="utf-8") == "y"

        # 停用后不再排期；删除后列表为空
        paused = rpc(ws, "cd", "cron.update", {"id": task["id"], "enabled": False})["result"]
        assert paused["enabled"] is False and paused["next_run_at"] == 0
        assert rpc(ws, "del", "cron.delete", {"id": task["id"]})["ok"]
        assert rpc(ws, "gl", "cron.list")["result"]["tasks"] == []


def test_e2_pipeline_dag_runs_in_dependency_order(home):
    """E2 编排：两个并行节点 + 一个汇总节点；汇总等到两者都完成并拿到上游产出。"""
    script = [
        [TextBlock(text="功能A完成")],
        [TextBlock(text="功能B完成")],
        [TextBlock(text="审查：两边都好")],
    ]
    with make_shared_client(home, script) as client, client.websocket_connect("/ws") as ws:
        pipe = rpc(ws, "c1", "pipeline.create", {
            "name": "登录模块", "concurrency": 2,
            "nodes": [
                {"title": "功能 A", "prompt": "做 A"},
                {"title": "功能 B", "prompt": "做 B"},
                {"title": "审查汇总", "prompt": "汇总审查", "after": [0, 1]},
            ],
        })["result"]["pipeline"]
        assert pipe["status"] == "draft", "创建后是草稿，等人启动"

        assert rpc(ws, "s1", "pipeline.start", {"id": pipe["id"]})["ok"]
        done = _wait_pipeline(ws, pipe["id"], ("done",))
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["功能 A"]["status"] == "done"
        assert by_title["功能 B"]["status"] == "done"
        assert by_title["审查汇总"]["result"] == "审查：两边都好"

        # 汇总节点的 prompt 注入了两个上游产出（依赖释放的实际证据）
        calls = getattr(client.app.state, "acceptance_provider", None)
        assert isinstance(calls, FakeProvider)
        injected = [
            m for m in calls.calls
            if m and getattr(m[-1], "role", "") == "user" and "前置任务" in m[-1].text
        ]
        assert injected, "汇总节点应收到注入了上游产出的 prompt"
        blob = injected[-1][-1].text
        assert "功能A完成" in blob and "功能B完成" in blob

        # 已完成流水线再次启动应给指引
        again = rpc(ws, "s2", "pipeline.start", {"id": pipe["id"]})
        assert again["ok"] is False
        assert "重跑" in again["error"] or "已完成" in again["error"]


def test_e3_subagent_report_and_cancel(home):
    """E3 子代理：同步派生拿到报告；后台任务可查、可取消。"""
    script = [
        [ToolUseBlock(id="s1", name="spawn_agent",
                      input={"agent_type": "explore", "prompt": "看看目录"})],
        [TextBlock(text="REPORT: 目录里有一个文件")],
        [TextBlock(text="子代理说目录里有一个文件")],
    ]
    with make_shared_client(home, script) as client, client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new")["result"]["id"]
        assert rpc(ws, "c1", "chat.send",
                   {"text": "派个子代理看看目录", "session_id": sid})["ok"]
        tasks = rpc(ws, "t1", "tasks.list")["result"]["tasks"]
        assert tasks, "子代理任务应出现在任务簿"
        task = tasks[0]
        assert task["agent_type"] == "explore"
        assert task["status"] == "done"
        detail = rpc(ws, "t2", "tasks.get", {"task_id": task["id"]})["result"]["task"]
        assert "目录里有一个文件" in detail["result"]

        # 归属：按别的会话过滤时看不到这个任务（远程连接的强制口径同源）
        other = rpc(ws, "n2", "session.new")["result"]["id"]
        scoped = rpc(ws, "t3", "tasks.list", {"session_id": other})["result"]["tasks"]
        assert scoped == []
        same = rpc(ws, "t3b", "tasks.list", {"session_id": sid})["result"]["tasks"]
        assert any(t["id"] == task["id"] for t in same)

        # 已终态的任务不能再取消：给可读错误，而不是假装成功
        twice = rpc(ws, "t4", "tasks.cancel", {"task_id": task["id"]})
        assert twice["ok"] is False and "finished" in twice["error"]


def test_e4_factory_is_local_only_and_survives_empty_state(home, monkeypatch):
    """E4 工厂：空项目也能出快照；远程连接一律拒绝。"""
    from skysheep.config import update_config_section

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        state = rpc(ws, "f1", "factory.state")["result"]
        assert "projects" in state and "counters" in state
        assert state["projects"], "当前项目应建出一个厂区"
        assert state["counters"]["running"] == 0
        assert rpc(ws, "dt", "factory.runs", {"limit": 5})["ok"]

    update_config_section("server", {"lan": True, "token": "tok-fac"})
    monkeypatch.setattr(server_app, "client_origin", lambda c: "other")
    monkeypatch.setattr(server_app, "_client_is_local", lambda c: False)
    with make_client(home, []) as client:
        with client.websocket_connect("/ws?token=tok-fac") as ws:
            for mid, method in (
                ("f2", "factory.state"),
                ("f3", "factory.runs"),
                ("f4", "factory.live"),
            ):
                out = rpc(ws, mid, method, {})
                assert out["ok"] is False and "本机" in out["error"]
