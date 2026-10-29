"""审计 SEC-M03 / SEC-M04 / SEC-M05 的远程作用域回归（2026-10-25 报告）。

- M03：远程 team.* 的 session_id 收敛到「连接绑定的会话」（_team_scoped_params
  用 _conn_session，不读全局 backend.session）；未绑定收敛为空串 fail closed；
  伪造他人 sid 无效；全局活动会话指针漂移不带动收敛对象。backend 侧 _team_for /
  team_get / team_log 对显式空串不回退全局活动会话（兜底）。
- M04：checkpoint.list/diff/restore 远程按连接绑定会话授权（照 tasks.list /
  runs.list 的 _remote_scope 口径），restore 不允许远程跨会话恢复；本机语义不变。
- M05：emit 对「无 session_id 的引擎级事件」改白名单（_REMOTE_ANON_KINDS）——
  cron_updated / pipeline_updated / map_updated 等携带其他项目 prompt、预授权
  工具名单、运行结果的整行事件默认不下发远端；会话事件（带 session_id）与
  白名单内事件（aux 流式、无载荷刷新信号、日程提醒）照常送达。

远程客户端的模拟方式与 test_security_hardening 一致：monkeypatch
_client_is_local（TestClient 的 scope client 固定为 testclient）。
"""

from __future__ import annotations

import functools

import pytest
from conftest import FakeProvider
from test_server import make_client, recv_until

import skysheep.server.app as server_app
from skysheep.messages import TextBlock, ToolUseBlock


class _FakeTeam:
    """最小团队替身：team.get 只调 snapshot()['team']，team.task_add 会真调。"""

    def __init__(self, team_id: str) -> None:
        self.team_id = team_id
        self.task_adds: list[str] = []

    def snapshot(self) -> dict:
        return {"team": {"team_id": self.team_id, "name": f"团队-{self.team_id}",
                         "members": [], "tasks": [], "messages": []}}

    async def task_add(self, title: str, assignee: str, **_kw) -> dict:
        self.task_adds.append(title)
        return {"id": "w1", "title": title, "assignee": assignee}


def _chat(ws, cid: str, text: str) -> dict:
    ws.send_json({"id": cid, "method": "chat.send", "params": {"text": text}})
    frame = recv_until(ws, cid)
    assert frame.get("ok"), frame
    return frame["result"]


# ---- M03：远程 team.* 的会话收敛 ----


def _bind_two_sessions(home, monkeypatch):
    """本机连接建会话 B（挂团队），远程连接绑定自己的会话 A；返回全部句柄。

    远程绑定走 session.new（前端「手机进来自建会话」的路径，连接跟踪按回执
    id 记订阅）：不带 session_id 的 chat.send 会跟随全局活动会话（手机镜像
    桌面当前会话的既有设计），这里必须显式绕开全局指针才能构造「连接绑定
    会话 ≠ 全局活动会话」的漂移场景。
    """
    calls = {"n": 0}

    def fake_local(client):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(server_app, "_client_is_local", fake_local)
    client = make_client(home, [])
    ws_local = client.websocket_connect("/ws")
    ws_remote = client.websocket_connect("/ws")
    return client, ws_local, ws_remote


def _session_new(ws, cid: str) -> str:
    ws.send_json({"id": cid, "method": "session.new", "params": {}})
    frame = recv_until(ws, cid)
    assert frame.get("ok"), frame
    return frame["result"]["id"]


def test_team_get_remote_converges_to_connection_session(home, monkeypatch):
    """M03 四情形：正常绑定 / 伪造他人 sid / 全局指针漂移 / 未绑定 fail closed。"""
    client, ws_local, ws_remote = _bind_two_sessions(home, monkeypatch)
    with client, ws_local, ws_remote:
        backend = client.app.state.backend
        sid_b = _chat(ws_local, "c0", "桌面会话")["session_id"]
        backend._teams[sid_b] = _FakeTeam("team-b")

        # 情形一（正常绑定）：远程自建会话 A，连接绑定到 A
        sid_a = _session_new(ws_remote, "n1")
        assert sid_a != sid_b

        # 情形三（伪造他人 sid）：断言 B 的 sid 被收敛为 A——A 还没有团队，
        # 返回空态且 session_id 是 A，B 的团队快照无从泄露
        ws_remote.send_json({"id": "t1", "method": "team.get",
                             "params": {"session_id": sid_b}})
        r = recv_until(ws_remote, "t1")["result"]
        assert r["active"] is False and r["session_id"] == sid_a

        # 情形一（正常绑定）：A 上建团后，连接只能看到自己会话的团队
        backend._teams[sid_a] = _FakeTeam("team-a")
        ws_remote.send_json({"id": "t2", "method": "team.get",
                             "params": {"session_id": sid_b}})
        r = recv_until(ws_remote, "t2")["result"]
        assert r["active"] is True and r["team"]["team_id"] == "team-a"

        # 情形四（全局指针漂移）：桌面把活动会话切到 B；远程 team.get 不带 sid
        # 仍收敛到连接绑定的 A，不随全局 backend.session 漂移
        ws_local.send_json({"id": "act", "method": "session.activate",
                            "params": {"id": sid_b}})
        assert recv_until(ws_local, "act")["ok"]
        ws_remote.send_json({"id": "t3", "method": "team.get", "params": {}})
        r = recv_until(ws_remote, "t3")["result"]
        assert r["active"] is True and r["team"]["team_id"] == "team-a"

        # 情形二（未绑定 fail closed）：新远程连接从未发起过会话请求，
        # 断言 B 的 sid 也拿不到 B 的团队，收敛对象是空串
        with client.websocket_connect("/ws") as ws_fresh:
            ws_fresh.send_json({"id": "t4", "method": "team.get",
                                "params": {"session_id": sid_b}})
            r = recv_until(ws_fresh, "t4")["result"]
            assert r["active"] is False and r["session_id"] == ""


def test_team_write_remote_cannot_touch_foreign_team(home, monkeypatch):
    """M03：远程 team.task_add 伪造他人 sid → 收敛后报「没有团队」，B 的板面无写。"""
    client, ws_local, ws_remote = _bind_two_sessions(home, monkeypatch)
    with client, ws_local, ws_remote:
        backend = client.app.state.backend
        sid_b = _chat(ws_local, "c0", "桌面会话")["session_id"]
        orch_b = _FakeTeam("team-b")
        backend._teams[sid_b] = orch_b
        _session_new(ws_remote, "n1")

        ws_remote.send_json({"id": "w1", "method": "team.task_add",
                             "params": {"session_id": sid_b, "title": "越权工单",
                                        "assignee": "队员甲"}})
        frame = recv_until(ws_remote, "w1")
        assert frame["ok"] is False and "没有进行中的团队" in frame["error"]
        assert orch_b.task_adds == [], "他人工单板不能被远程写入"


def test_team_log_remote_cannot_replay_foreign_team(home, monkeypatch):
    """M03：team.log 伪造他人 sid / 未绑定都拿不到他人团队的频道消息。"""
    client, ws_local, ws_remote = _bind_two_sessions(home, monkeypatch)
    with client, ws_local, ws_remote:
        backend = client.app.state.backend
        sid_b = _chat(ws_local, "c0", "桌面会话")["session_id"]
        backend._teams[sid_b] = _FakeTeam("team-b")
        # 频道里给 B 的团队落一条「机密」消息（session_id 归属 B）
        client.portal.call(functools.partial(
            backend.store.add_team_message,
            "team-b", 1, "director", "all",
            text="机密频道消息", session_id=sid_b,
        ))
        _session_new(ws_remote, "n1")

        ws_remote.send_json({"id": "l1", "method": "team.log",
                             "params": {"session_id": sid_b, "team_id": "team-b"}})
        frame = recv_until(ws_remote, "l1")
        assert frame["ok"] is False and "不属于当前会话" in frame["error"]

        # 未绑定的远程连接同样拿不到（sid 收敛为空串，落库行全是他人的）
        with client.websocket_connect("/ws") as ws_fresh:
            ws_fresh.send_json({"id": "l2", "method": "team.log",
                                "params": {"session_id": sid_b, "team_id": "team-b"}})
            frame = recv_until(ws_fresh, "l2")
            assert frame["ok"] is False and "不属于当前会话" in frame["error"]


def test_team_local_still_reaches_active_and_explicit_session(home):
    """本机语义不变：不带 sid 用活动会话，显式 sid 直达（前端既有用法）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        sid_b = _chat(ws, "c0", "本机会话")["session_id"]
        backend._teams[sid_b] = _FakeTeam("team-b")
        ws.send_json({"id": "t1", "method": "team.get", "params": {}})
        r = recv_until(ws, "t1")["result"]
        assert r["active"] is True and r["team"]["team_id"] == "team-b"
        ws.send_json({"id": "t2", "method": "team.get",
                      "params": {"session_id": sid_b}})
        r = recv_until(ws, "t2")["result"]
        assert r["active"] is True and r["team"]["team_id"] == "team-b"


# ---- M04：远程 checkpoint.* 的会话授权 ----


def test_checkpoint_remote_scoped_to_connection_session(home, monkeypatch):
    """M04：远程 list/diff/restore 只对连接绑定会话生效，伪造他人 cp id 无效。"""
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "cp-b.txt", "content": "B的改动"})],
        [TextBlock(text="written")],
        [TextBlock(text="回应")],
        [TextBlock(text="回应")],
    ])
    calls = {"n": 0}

    def fake_local(client):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(server_app, "_client_is_local", fake_local)
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws_local, \
            client.websocket_connect("/ws") as ws_remote:
        backend = client.app.state.backend

        # 桌面（本机）在会话 B 写文件 → 产生检查点
        ws_local.send_json({"id": "c0", "method": "chat.send", "params": {"text": "写文件"}})
        cp_b = None
        while cp_b is None:
            fr = ws_local.receive_json()
            if "event" in fr:
                if fr["event"] == "permission_request":
                    ws_local.send_json({"id": "pa", "method": "permission.respond",
                                        "params": {"request_id": fr["data"]["request_id"],
                                                   "decision": "allow_once"}})
                continue
            if fr.get("id") == "c0":
                assert fr["ok"], fr.get("error")
                cp_b = fr["result"]["checkpoint"]
        sid_b = fr["result"]["session_id"]
        target = home / "proj" / "cp-b.txt"
        assert target.exists()

        # 远程绑定自己的会话 A（session.new 即绑，不消耗 provider 脚本）
        sid_a = _session_new(ws_remote, "n1")
        assert sid_a != sid_b

        # list：远程只看 A 的检查点（A 没有改动 → 空），拿不到 B 的 cp id
        ws_remote.send_json({"id": "l1", "method": "checkpoint.list", "params": {}})
        rows = recv_until(ws_remote, "l1")["result"]["checkpoints"]
        assert rows == []

        # diff：凭枚举的 B 的 cp id 读不到改前内容
        ws_remote.send_json({"id": "d1", "method": "checkpoint.diff",
                             "params": {"id": cp_b["id"]}})
        frame = recv_until(ws_remote, "d1")
        assert frame["ok"] is False

        # restore：远程不能把 B 项目工作区的文件覆盖回改前快照
        ws_remote.send_json({"id": "r1", "method": "checkpoint.restore",
                             "params": {"id": cp_b["id"], "force": True}})
        frame = recv_until(ws_remote, "r1")
        assert frame["ok"] is False
        assert target.exists(), "文件没有被远程回滚"

        # 伪造他人 sid 同样无效：断言 B 的会话 id 不参与授权
        ws_remote.send_json({"id": "r2", "method": "checkpoint.restore",
                             "params": {"id": cp_b["id"], "session_id": sid_b,
                                        "force": True}})
        frame = recv_until(ws_remote, "r2")
        assert frame["ok"] is False
        assert target.exists()

        # 全局指针漂移：桌面把活动会话切到 B 后，远程 checkpoint.list 仍收敛到 A
        ws_local.send_json({"id": "act", "method": "session.activate",
                            "params": {"id": sid_b}})
        assert recv_until(ws_local, "act")["ok"]
        ws_remote.send_json({"id": "l2", "method": "checkpoint.list", "params": {}})
        rows = recv_until(ws_remote, "l2")["result"]["checkpoints"]
        assert rows == []
        assert backend.session.id == sid_b  # 前置成立：全局指针确实在 B 上

        # 本机不受影响：桌面仍能看到并回滚 B 自己的检查点
        ws_local.send_json({"id": "l3", "method": "checkpoint.list", "params": {}})
        rows = recv_until(ws_local, "l3")["result"]["checkpoints"]
        assert cp_b["id"] in [c["id"] for c in rows]
        ws_local.send_json({"id": "r3", "method": "checkpoint.restore",
                            "params": {"id": cp_b["id"]}})
        frame = recv_until(ws_local, "r3")
        assert frame["ok"] and frame["result"]["files"]
        assert not target.exists(), "新建文件的回滚是删除"


def test_checkpoint_remote_unbound_fail_closed(home, monkeypatch):
    """M04：未绑定会话的远程连接 list 回空、diff/restore 直接拒绝。"""
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "checkpoint.list", "params": {}})
        assert recv_until(ws, "l1")["result"]["checkpoints"] == []
        ws.send_json({"id": "d1", "method": "checkpoint.diff", "params": {"id": "cp1"}})
        assert recv_until(ws, "d1")["ok"] is False
        ws.send_json({"id": "r1", "method": "checkpoint.restore", "params": {"id": "cp1"}})
        assert recv_until(ws, "r1")["ok"] is False


def test_checkpoint_remote_can_use_own_session_checkpoint(home, monkeypatch):
    """M04 不误伤：远程绑定会话自己的检查点照常 list/diff/restore。"""
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "cp-a.txt", "content": "远程的改动"})],
        [TextBlock(text="written")],
    ])
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        cp = None
        while cp is None:
            fr = ws.receive_json()
            if "event" in fr:
                if fr["event"] == "permission_request":
                    ws.send_json({"id": "pa", "method": "permission.respond",
                                  "params": {"request_id": fr["data"]["request_id"],
                                             "decision": "allow_once"}})
                continue
            if fr.get("id") == "c1":
                assert fr["ok"], fr.get("error")
                cp = fr["result"]["checkpoint"]
        target = home / "proj" / "cp-a.txt"
        assert target.exists()

        ws.send_json({"id": "l1", "method": "checkpoint.list", "params": {}})
        rows = recv_until(ws, "l1")["result"]["checkpoints"]
        assert cp["id"] in [c["id"] for c in rows]

        ws.send_json({"id": "d1", "method": "checkpoint.diff", "params": {"id": cp["id"]}})
        diff = recv_until(ws, "d1")
        assert diff["ok"] and diff["result"]["files"]

        ws.send_json({"id": "r1", "method": "checkpoint.restore", "params": {"id": cp["id"]}})
        frame = recv_until(ws, "r1")
        assert frame["ok"] and frame["result"]["files"]
        assert not target.exists()


# ---- M05：emit 远程过滤改白名单 ----


def test_emit_engine_level_events_blocked_for_remote(home, monkeypatch):
    """M05：无会话归属的引擎级事件默认不下发远端（cron/pipeline/map 整行等）。"""
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend

        async def broadcast():
            payloads = (
                {"kind": "cron_updated",
                 "task": {"id": 1, "name": "任务", "prompt": "无人值守指令全文",
                          "allowed_tools": ["write_file", "run_command"],
                          "last_result": "运行结果摘要"}},
                {"kind": "pipeline_updated",
                 "pipeline": {"name": "产线", "nodes": [{"title": "节点", "prompt": "节点指令"}]}},
                {"kind": "map_updated", "project_id": 9, "ok": True, "message": "已生成"},
                {"kind": "subagent_event", "text": "子代理直播"},
                {"kind": "subagent_spawned"},
                {"kind": "task_finished"},
                {"kind": "factory_run", "run": {"id": 1}},
                {"kind": "term_data", "term_id": "t1", "text": "机密终端输出"},
                {"kind": "term_exit", "term_id": "t1"},
                {"kind": "memory_maintain", "scope": "project", "message": "x"},
            )
            for ev in payloads:
                for emitter in list(backend.ws_emitters):
                    await emitter(ev)

        client.portal.call(broadcast)
        events: list = []
        ws.send_json({"id": "barrier", "method": "boot"})
        assert recv_until(ws, "barrier", events)["ok"]
        assert events == [], f"引擎级整行事件不应到达远端：{[e['event'] for e in events]}"


def test_emit_whitelist_and_session_events_still_reach_remote(home, monkeypatch):
    """M05 不误伤：白名单事件与会话事件（permission/delta/用户消息）照常送达。"""
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        sid = _chat(ws, "c1", "绑定会话")["session_id"]

        async def broadcast():
            payloads = (
                {"kind": "mcp_updated"},
                {"kind": "mods_updated"},
                {"kind": "schedule_updated"},
                {"kind": "schedule_reminder", "id": 1, "title": "喝水", "notes": ""},
                {"kind": "aux_delta", "aux_id": "aux1", "text": "远程辅助流"},
                {"kind": "aux_thinking", "aux_id": "aux1", "text": "思考"},
                # 会话事件：归属过滤只认连接绑定的会话
                {"kind": "text_delta", "session_id": sid, "text": "增量"},
                {"kind": "user_message", "session_id": sid, "text": "会话消息"},
                {"kind": "permission_request", "session_id": sid,
                 "request_id": "rid-1", "tool_name": "write_file"},
            )
            for ev in payloads:
                for emitter in list(backend.ws_emitters):
                    await emitter(ev)

        client.portal.call(broadcast)
        events: list = []
        ws.send_json({"id": "barrier", "method": "boot"})
        assert recv_until(ws, "barrier", events)["ok"]
        kinds = [e["event"] for e in events]
        for expected in ("mcp_updated", "mods_updated", "schedule_updated",
                         "schedule_reminder", "aux_delta", "aux_thinking",
                         "text_delta", "user_message", "permission_request"):
            assert expected in kinds, f"{expected} 应照常送达远端"
        # 他人会话的事件仍被连接归属过滤
        foreign = [e for e in events
                   if e.get("data", {}).get("session_id") not in (sid, None)]
        assert foreign == []

        # permission_request 到达远端后登记进 remote_permissions：可应答
        delivered: list = []
        monkeypatch.setattr(backend, "respond_permission",
                            lambda rid, decision: delivered.append(rid) or True)
        ws.send_json({"id": "p1", "method": "permission.respond",
                      "params": {"request_id": "rid-1", "decision": "allow_once"}})
        assert recv_until(ws, "p1")["result"] == {"delivered": True}
        assert delivered == ["rid-1"]


def test_emit_local_connections_still_receive_everything(home):
    """M05 对照组：本机连接不过滤——cron/pipeline/map 等引擎级事件照收。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend

        async def broadcast():
            for ev in ({"kind": "cron_updated", "task": {"id": 1}},
                       {"kind": "pipeline_updated", "pipeline": {"name": "x"}},
                       {"kind": "map_updated", "project_id": 1, "ok": True},
                       {"kind": "term_data", "term_id": "t", "text": "out"}):
                for emitter in list(backend.ws_emitters):
                    await emitter(ev)

        client.portal.call(broadcast)
        events: list = []
        ws.send_json({"id": "barrier", "method": "boot"})
        assert recv_until(ws, "barrier", events)["ok"]
        kinds = [e["event"] for e in events]
        assert kinds == ["cron_updated", "pipeline_updated", "map_updated", "term_data"]


@pytest.mark.parametrize("kind", ["cron_updated", "pipeline_updated", "map_updated"])
def test_emit_reported_kinds_are_not_whitelisted(kind):
    """M05 锚点：报告点名的三类整行广播必须留在白名单外（防误登记）。"""
    from skysheep.server.app import _REMOTE_ANON_KINDS

    assert kind not in _REMOTE_ANON_KINDS


# ---- 审查 L06：advanced.get / hooks.get 的本机绝对路径不外泄远端 ----


def test_advanced_get_masks_local_paths_for_remote():
    import asyncio

    from skysheep.server._dispatch_prefs import _h_advanced_get

    class _Backend:
        def advanced_settings(self):
            return {"home": "/home/x", "logs_dir": "/home/x/logs",
                    "working_dir": "/work", "autostart": {"enabled": True}}

    out = asyncio.run(_h_advanced_get(_Backend(), {}, None, False))
    assert out["home"] == "" and out["logs_dir"] == "" and out["working_dir"] == ""
    assert out["autostart"] == {"enabled": True}  # 功能面照回，远程设置页仍可读
    out_local = asyncio.run(_h_advanced_get(_Backend(), {}, None, True))
    assert out_local["home"] == "/home/x" and out_local["working_dir"] == "/work"


def test_hooks_get_masks_config_path_for_remote():
    import asyncio

    from skysheep.server._dispatch_ops import _h_hooks_get

    class _Backend:
        def hooks_settings(self):
            return {"pre": [], "config_path": "/home/x/.skysheep/config.toml"}

    out = asyncio.run(_h_hooks_get(_Backend(), {}, None, False))
    assert out["config_path"] == ""
    out_local = asyncio.run(_h_hooks_get(_Backend(), {}, None, True))
    assert out_local["config_path"].endswith("config.toml")


# ---- 审查 L08：queue 命中的未决请求登记进 remote_permissions，重连后可应答 ----


def test_queue_result_registers_permissions_for_respond():
    from skysheep.server.app import _register_queue_permissions

    rp: dict[str, str] = {}
    _register_queue_permissions(rp, {"pending": [
        {"request_id": "r1", "session_id": "sA"},
        {"request_id": "", "session_id": "sA"},  # 解析不出 rid 的不登记
        "junk",                                   # 非字典行忽略
        {"request_id": "r2"},                     # 无 session_id 按空会话登记
    ]})
    assert rp == {"r1": "sA", "r2": ""}
    _register_queue_permissions(rp, None)
    assert rp == {"r1": "sA", "r2": ""}           # None / 空结果安全
