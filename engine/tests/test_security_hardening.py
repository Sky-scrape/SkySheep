"""安全审查（2026-09 汇总）三层修复的回归测试。

覆盖（按汇总的修复方案）：
1. dispatch 层本机门禁：LOCAL_ONLY_METHODS 里的配置/管理类 RPC 对远程客户端一律
   拒绝；lan/remote.status 对远程不回传令牌（A 族 + A9/A16）。
2. Store/Backend 层归属校验：跨项目的会话操作（chat.send/refs/export/元数据/删除/
   移动）、Cron、白名单规则、用量统计全部拒绝或过滤（B 族）；子代理任务查询与
   直播事件按会话隔离（B13/B15）。
3. 边界隔离：非同源 Origin 的 WS 握手拒绝、预览 iframe 带 sandbox 且不含
   allow-same-origin（D5）；clipboard_read 升级为需确认（D4）。

远程客户端的模拟方式与 test_permission_hardening 一致：monkeypatch
_client_is_local（TestClient 的 scope client 固定为 testclient，无法从外部
换成局域网地址）。
"""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect
from test_server import make_client, recv_until  # noqa: F401

import skysheep.server.app as server_app
from skysheep.server.app import (
    LOCAL_ONLY_METHODS,
    STATIC_DIR,
    _client_is_local,
    _is_hidden_static_path,
)

# ---- 1. dispatch 层本机门禁 ----


def test_local_only_methods_rejected_for_remote(home, monkeypatch):
    """LOCAL_ONLY_METHODS 全量：远程调用一律拒绝，错误说明只能本机操作。"""
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i, method in enumerate(sorted(LOCAL_ONLY_METHODS)):
            ws.send_json({"id": f"m{i}", "method": method, "params": {}})
            frame = recv_until(ws, f"m{i}")
            assert frame["ok"] is False, method
            assert "本机" in frame["error"], method


def test_local_only_covers_config_and_rce_surface():
    """关键高危面必须都在门禁表里（A1-A16 的抽查锚点，防止表被误删减）。"""
    must = {
        "mcp.save_server", "mcp.import",           # A1：写任意 command 即启动本地程序
        "config.probe_models", "config.save_provider",  # A2/A3：Key 外发
        "websearch.save", "imagegen.save", "speech.save",  # A4/A5/A13
        "hooks.save",                              # A6：钩子命令不经权限门执行
        "cron.add", "cron.update", "cron.run_now",  # A7：预授权工具 + 无人值守
        "settings.export", "settings.import",      # A8/A15：明文 Key 打包
        "skills.install", "skills.delete",         # A10：技能正文进 system prompt
        "lan.enable", "remote.enable",             # A11
        "config.add_provider",                     # A12
        "memory.save",                             # A14：持久注入
        "project.switch", "project.delete", "project.save_instructions",  # C1/C2/C3
        "advanced.save",                           # A16：安全姿态开关
    }
    assert must <= LOCAL_ONLY_METHODS


def test_local_client_still_saves_memory(home):
    """本机客户端不受门禁影响（桌面端设置页照常工作）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "memory.save", "params": {"text": "偏好：简洁"}})
        assert recv_until(ws, "m1")["ok"] is True


def test_lan_status_hides_token_from_remote(home, monkeypatch):
    """lan/remote.status：远程只回 has_token，不回传令牌本体（A9）。"""
    from skysheep.config import update_config_section

    update_config_section("server", {"token": "tok-secret-abcdef123456"})
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "lan.status"})
        st = recv_until(ws, "l1")["result"]
        assert st["token"] == "" and st["has_token"] is True
        ws.send_json({"id": "r1", "method": "remote.status"})
        st = recv_until(ws, "r1")["result"]
        assert st["token"] == "" and st["has_token"] is True
    # 只恢复本机判定函数本身，不动 home fixture 的环境变量（undo 会连 SKYSHEEP_HOME 一起撤）
    monkeypatch.setattr(server_app, "_client_is_local", _client_is_local)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l2", "method": "lan.status"})
        st = recv_until(ws, "l2")["result"]
        assert st["token"] == "tok-secret-abcdef123456"


def test_remote_cannot_search_across_projects(home, monkeypatch):
    """跨项目搜索（scope=all）只留给本机：远程枚举其他项目会话的入口关掉。"""
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "session.search",
                      "params": {"query": "x", "scope": "all"}})
        frame = recv_until(ws, "s1")
        assert frame["ok"] is False and "本机" in frame["error"]
        ws.send_json({"id": "s2", "method": "session.search",
                      "params": {"query": "x", "scope": "project"}})
        assert recv_until(ws, "s2")["ok"] is True


def test_remote_project_list_and_backups_strip_paths(home, monkeypatch):
    """远程视角：项目列表不带根路径（C5），备份列表不带绝对路径与目录（C4）。"""
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "project.list"})
        projects = recv_until(ws, "p1")["result"]["projects"]
        assert projects and all(p["root_path"] == "" for p in projects)
        assert any(p["is_current"] for p in projects)  # 名称与当前标记仍可用
        ws.send_json({"id": "b1", "method": "session.backups"})
        out = recv_until(ws, "b1")["result"]
        assert out["dir"] == "" and all(b["path"] == "" for b in out["backups"])


# ---- 2. Store/Backend 层归属校验 ----


async def test_store_get_session_for_project(store):
    """store 层归属查询：跨项目（含快聊边界）返回 None（B 族的地基）。"""
    pa = await store.get_or_create_project("/a")
    pb = await store.get_or_create_project("/b")
    sa = await store.create_session(pa.id, "A 的会话")
    sb = await store.create_session(pb.id, "B 的会话")
    squick = await store.create_session(None, "快聊会话")

    assert (await store.get_session_for_project(sa.id, pa.id)).id == sa.id
    assert (await store.get_session_for_project(sb.id, pb.id)).id == sb.id
    assert await store.get_session_for_project(sa.id, pb.id) is None      # 跨项目
    assert await store.get_session_for_project(sb.id, pa.id) is None      # 反向同理
    assert await store.get_session_for_project(squick.id, None) is not None  # 快聊归快聊
    assert await store.get_session_for_project(squick.id, pa.id) is None     # 快聊不进项目
    assert await store.get_session_for_project("nope", pa.id) is None


def test_cross_project_session_ops_denied(home):
    """跨项目会话操作全链路拒绝：activate/chat.send/export/元数据/删除/移动。"""
    from skysheep.messages import TextBlock

    (home / "proj2").mkdir()
    script = [[TextBlock(text="ok")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        # 去项目 B 建一个会话（本机客户端可以自由切换）
        ws.send_json({"id": "sw1", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw1")["ok"]
        ws.send_json({"id": "n1", "method": "session.new"})
        sid_b = recv_until(ws, "n1")["result"]["id"]
        # 回到项目 A
        ws.send_json({"id": "sw2", "method": "project.switch",
                      "params": {"path": str(home / "proj")}})
        assert recv_until(ws, "sw2")["ok"]

        # 从项目 A 操作 B 的会话：全部拒绝（B1/B3/B4/B6/B7/B10）
        cases = [
            ("session.activate", {"id": sid_b}),
            ("session.resume", {"id": sid_b}),
            ("session.export", {"id": sid_b, "fmt": "md"}),
            ("session.rename", {"id": sid_b, "title": "偷改标题"}),
            ("session.pin", {"id": sid_b, "pinned": True}),
            ("session.archive", {"id": sid_b, "archived": True}),
            ("session.tags", {"id": sid_b, "tags": ["偷打标签"]}),
            ("session.move", {"id": sid_b, "project_id": None}),
            ("session.delete", {"id": sid_b}),
            ("chat.send", {"text": "hi", "session_id": sid_b}),
        ]
        for i, (method, params) in enumerate(cases):
            ws.send_json({"id": f"c{i}", "method": method, "params": params})
            frame = recv_until(ws, f"c{i}")
            assert frame["ok"] is False, method
            assert "not found" in frame["error"], method

        # B 的会话没有被动过（去 B 里还能激活）
        ws.send_json({"id": "sw3", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw3")["ok"]
        ws.send_json({"id": "a1", "method": "session.activate", "params": {"id": sid_b}})
        assert recv_until(ws, "a1")["ok"]


def test_refs_drop_foreign_project_sessions(home):
    """& 引用：同项目会话照常注入，其他项目的会话静默剔除（B2）。"""
    from skysheep.messages import TextBlock

    (home / "proj2").mkdir()
    script = [
        [TextBlock(text="暗号是 pineapple-secret")],   # S1（项目 A）
        [TextBlock(text="foreign-secret-xyz")],        # S_foreign（项目 B）
        [TextBlock(text="引用已处理")],                 # S2（项目 A，带 refs）
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "chat.send", "params": {"text": "S1 的内容"}})
        sid_a = recv_until(ws, "t1")["result"]["session_id"]
        # 切到 B 开会话聊一句，拿到 B 的会话 id
        ws.send_json({"id": "sw1", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw1")["ok"]
        ws.send_json({"id": "t2", "method": "chat.send", "params": {"text": "B 的内容"}})
        sid_b = recv_until(ws, "t2")["result"]["session_id"]
        ws.send_json({"id": "sw2", "method": "project.switch",
                      "params": {"path": str(home / "proj")}})
        assert recv_until(ws, "sw2")["ok"]

        # 新会话同时引用 A/B 两个会话：只有 A 的被注入
        ws.send_json({"id": "t3", "method": "chat.send",
                      "params": {"text": "看看引用", "refs": [sid_a, sid_b]}})
        frame = recv_until(ws, "t3")
        assert frame["ok"]
        sid_new = frame["result"]["session_id"]
        ws.send_json({"id": "e1", "method": "session.export",
                      "params": {"id": sid_new, "fmt": "md"}})
        md = recv_until(ws, "e1")["result"]["markdown"]
        assert "pineapple-secret" in md       # 同项目引用正常生效
        assert "foreign-secret-xyz" not in md  # 跨项目引用被剔除


def test_cron_cross_project_denied(home):
    """Cron 的改/触发/删只对当前项目的任务生效（B8）。"""
    (home / "proj2").mkdir()
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "sw1", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw1")["ok"]
        ws.send_json({"id": "ca1", "method": "cron.add", "params": {
            "name": "B 的任务", "prompt": "p", "schedule_type": "interval",
            "interval_minutes": 30,
        }})
        tid = recv_until(ws, "ca1")["result"]["id"]
        ws.send_json({"id": "sw2", "method": "project.switch",
                      "params": {"path": str(home / "proj")}})
        assert recv_until(ws, "sw2")["ok"]

        for i, (method, params) in enumerate([
            ("cron.update", {"id": tid, "name": "偷改"}),
            ("cron.run_now", {"id": tid}),
            ("cron.delete", {"id": tid}),
        ]):
            ws.send_json({"id": f"cc{i}", "method": method, "params": params})
            frame = recv_until(ws, f"cc{i}")
            assert frame["ok"] is False, method
            assert "不存在" in frame["error"], method

        # 任务仍在（回 B 还能列出）
        ws.send_json({"id": "sw3", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw3")["ok"]
        ws.send_json({"id": "cl", "method": "cron.list"})
        tasks = recv_until(ws, "cl")["result"]["tasks"]
        assert any(t["id"] == tid for t in tasks)


def test_whitelist_remove_scoped_to_project(home):
    """白名单删除带项目条件：别的项目的规则删不掉（B11）。"""
    (home / "proj2").mkdir()
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "wa", "method": "whitelist.add", "params": {
            "tool": "run_command", "kind": "prefix", "pattern": "git status",
        }})
        rules = recv_until(ws, "wa")["result"]["rules"]
        rule_id = rules[0]["id"]

        ws.send_json({"id": "sw1", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw1")["ok"]
        ws.send_json({"id": "wr", "method": "whitelist.remove", "params": {"id": rule_id}})
        frame = recv_until(ws, "wr")
        assert frame["ok"] is False and "不存在" in frame["error"]

        # 回到原项目：正常删除
        ws.send_json({"id": "sw2", "method": "project.switch",
                      "params": {"path": str(home / "proj")}})
        assert recv_until(ws, "sw2")["ok"]
        ws.send_json({"id": "wr2", "method": "whitelist.remove", "params": {"id": rule_id}})
        assert recv_until(ws, "wr2")["ok"]


async def test_usage_stats_scoped_to_project(store):
    """用量统计按项目过滤：by_session 只含当前项目的会话（B14）。"""
    pa = await store.get_or_create_project("/a")
    pb = await store.get_or_create_project("/b")
    sa = await store.create_session(pa.id, "A 会话")
    sb = await store.create_session(pb.id, "B 会话")
    await store.add_usage(sa.id, "p1", "m1", 100, 50)
    await store.add_usage(sb.id, "p1", "m1", 700, 30)
    scoped = await store.usage_stats(14, project_id=pa.id)
    sids = [r["sid"] for r in scoped["by_session"]]
    assert sids == [sa.id]
    assert sum(r["it"] for r in scoped["by_provider"]) == 100

    # 缺省不过滤（CLI / 全局场景的行为保持不变）
    full = await store.usage_stats(14)
    assert {r["sid"] for r in full["by_session"]} == {sa.id, sb.id}


def test_checkpoint_restore_checks_session_ownership(home):
    """检查点凭 id 恢复/对比前先验会话归属（B12：cp id 顺序可枚举）。

    同项目另一个会话的快照也不能恢复——恢复是写盘原语；对比会返回文件内容。
    """
    from skysheep.messages import TextBlock, ToolUseBlock

    script = [
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "a.txt", "content": "v1"})],
        [TextBlock(text="已写入")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        # 开自动写入，让 write_file 不弹确认（本机客户端可以）
        ws.send_json({"id": "pm", "method": "permission.set_mode",
                      "params": {"mode": "accept_edits"}})
        assert recv_until(ws, "pm")["ok"]
        # 第一轮：写入文件 → 产生 S1 的检查点
        ws.send_json({"id": "t1", "method": "chat.send", "params": {"text": "写文件"}})
        frame = recv_until(ws, "t1")
        assert frame["ok"] and frame["result"]["checkpoint"]
        cp_id = frame["result"]["checkpoint"]["id"]

        # 同一会话内：恢复可用（正常产品路径）
        ws.send_json({"id": "r0", "method": "checkpoint.restore", "params": {"id": cp_id}})
        assert recv_until(ws, "r0")["ok"]

        # 切到新会话 S2：枚举到的 S1 检查点不能再恢复/对比
        ws.send_json({"id": "n1", "method": "session.new"})
        assert recv_until(ws, "n1")["ok"]
        for i, method in enumerate(["checkpoint.restore", "checkpoint.diff"]):
            ws.send_json({"id": f"cr{i}", "method": method, "params": {"id": cp_id}})
            frame = recv_until(ws, f"cr{i}")
            assert frame["ok"] is False, method
            assert "会话" in frame["error"], method
        # 列表本身也只显示当前会话的检查点
        ws.send_json({"id": "cl", "method": "checkpoint.list"})
        assert recv_until(ws, "cl")["result"]["checkpoints"] == []


# ---- 3. 子代理任务的会话隔离（B13/B15） ----


async def test_taskmanager_session_scoping(tmp_path):
    """任务簿按会话过滤：list/get/cancel 只作用于指定会话的任务。"""
    from skysheep.core.subagent import TaskManager
    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider

    fp = FakeProvider([]).with_default([TextBlock(text="done")])
    tasks = TaskManager(provider_factory=lambda: fp, working_dir=tmp_path)
    tasks.set_active_session("sess-1")
    tid1 = tasks.start_background("explore", "任务一")
    tasks.set_active_session("sess-2")
    tid2 = tasks.start_background("explore", "任务二")
    # 等两个任务都跑完（过滤对终态记录同样生效）
    for _ in range(100):
        if tasks.status(tid1).status == "done" and tasks.status(tid2).status == "done":
            break
        import asyncio

        await asyncio.sleep(0.02)

    only1 = tasks.list_tasks(session_id="sess-1")
    assert [t["id"] for t in only1] == [tid1]
    assert only1[0]["session_id"] == "sess-1"
    assert tasks.get_detail(tid2, session_id="sess-1") is None   # 别的会话查不到
    assert tasks.get_detail(tid2, session_id="sess-2") is not None
    assert tasks.get_detail(tid2) is not None                    # 不过滤时保持全局视图


async def test_subagent_events_carry_session_id(tmp_path):
    """直播与终态事件带 session_id：服务层据此做连接级隔离（B15 的数据面）。"""
    from skysheep.core.subagent import TaskManager
    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider

    fp = FakeProvider([]).with_default([TextBlock(text="report")])
    seen: list[dict] = []
    tasks = TaskManager(
        provider_factory=lambda: fp,
        working_dir=tmp_path,
        event_emitter=lambda ev: seen.append(ev),
    )
    tasks.set_active_session("sess-x")
    tasks.start_background("explore", "调研")
    for _ in range(100):
        if any(e["kind"] == "task_finished" for e in seen):
            break
        import asyncio

        await asyncio.sleep(0.02)
    subs = [e for e in seen if e["kind"] == "subagent_event"]
    fins = [e for e in seen if e["kind"] == "task_finished"]
    assert subs and all(e["session_id"] == "sess-x" for e in subs)
    assert fins and all(e["session_id"] == "sess-x" for e in fins)


def test_subagent_live_events_isolated_for_remote(home, monkeypatch):
    """端到端：远程连接收不到其他会话的子代理直播（B15 的连接面）。"""
    from skysheep.messages import TextBlock, ToolUseBlock

    script = [
        [ToolUseBlock(id="sp1", name="spawn_agent",
                      input={"agent_type": "explore", "prompt": "看看目录"})],
        [TextBlock(text="子代理报告完成")],
        [TextBlock(text="主轮完成")],
    ]
    # 第一个连接算本机，之后的连接算远程（_client_is_local 按调用次序分流）
    calls = {"n": 0}

    def fake_local(client):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(server_app, "_client_is_local", fake_local)
    with make_client(home, script) as client, \
         client.websocket_connect("/ws") as ws_local, \
         client.websocket_connect("/ws") as ws_remote:
        ws_remote.send_json({"id": "b2", "method": "boot"})
        assert recv_until(ws_remote, "b2")["ok"]

        ws_local.send_json({"id": "c1", "method": "chat.send", "params": {"text": "派个子代理"}})
        ev_local = []
        frame = recv_until(ws_local, "c1", ev_local)
        assert frame["ok"] and frame["result"]["done"]
        sid = frame["result"]["session_id"]
        # 本机连接（不过滤）：能看到直播，且事件带 session_id
        subs = [e for e in ev_local if e["event"] == "subagent_event"]
        assert subs and all(e["data"]["session_id"] == sid for e in subs)

        # 远程连接（从未交互过任何会话）：直播被过滤，连一帧都不该出现
        ev_remote = []
        ws_remote.send_json({"id": "s2", "method": "chat.status"})
        recv_until(ws_remote, "s2", ev_remote)
        assert "subagent_event" not in [e["event"] for e in ev_remote]
        assert "task_finished" not in [e["event"] for e in ev_remote]


# ---- 4. WS Origin 校验与 preview 隔离（D5） ----


def test_ws_rejects_foreign_origin(home):
    """Origin 与 Host 不一致的 WS 握手（外部页面/沙箱 iframe）直接 4403 关闭。"""
    with make_client(home, []) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws", headers={"Origin": "http://evil.example"}
            ) as ws:
                ws.receive_json()
        assert exc_info.value.code == 4403


def test_ws_allows_matching_origin(home):
    """Origin 与 Host 一致（应用自己的页面）：正常连接。"""
    with make_client(home, []) as client:
        with client.websocket_connect(
            "/ws", headers={"Origin": "http://testserver"}  # TestClient 的 Host 是 testserver
        ) as ws:
            ws.send_json({"id": "b1", "method": "boot"})
            assert recv_until(ws, "b1")["ok"]


def test_preview_frame_has_sandbox():
    """预览 iframe 必须带 sandbox 且绝不含 allow-same-origin（D5 主防线）。"""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1]
            / "src" / "skysheep" / "server" / "static" / "index.html").read_text(
                encoding="utf-8")
    import re

    m = re.search(r'<iframe id="browser-frame"[^>]*>', html)
    assert m, "browser-frame iframe 不见了？"
    tag = m.group(0)
    assert "sandbox" in tag
    assert "allow-same-origin" not in tag
    assert "allow-scripts" in tag  # 预览的页面脚本仍要能跑（本地开发服务器）


def test_preview_response_carries_sandbox_csp(home):
    """/preview 响应自带 CSP sandbox 头（iframe 之外打开也是隔离环境）。"""
    with make_client(home, []) as client:
        target = home / "proj" / "demo.html"
        target.write_text("<p>hi</p>", encoding="utf-8")
        resp = client.get("/preview", params={"p": str(target)})
        assert resp.status_code == 200
        assert "sandbox" in resp.headers.get("content-security-policy", "")
        assert resp.headers.get("x-content-type-options") == "nosniff"


# ---- 5. 工具安全分级复核（D4） ----


def test_clipboard_read_requires_confirmation():
    """clipboard_read 从只读降为需确认：剪贴板是高敏感数据源。"""
    from skysheep.tools.base import Safety
    from skysheep.tools.computer import ClipboardReadTool

    assert ClipboardReadTool.safety == Safety.WRITE


def test_static_hidden_paths_not_served(home):
    """static 下的点开头路径一律 404。

    StaticFiles 不拒隐藏路径：开发期目录（.mimosa/ 等）一旦被误打进安装包，
    就会随 /static 挂载变成未认证可读（1.0/1.5/1.8 安装包正是这样泄露了
    变更哈希、会话 id 与源码快照）。打包侧已跳过点开头路径，这里是运行期
    的第二道防线，避免构建配置回退时重现同一问题。
    """
    import shutil

    hidden_dir = STATIC_DIR / ".mimosa" / "reports"
    shutil.rmtree(STATIC_DIR / ".mimosa", ignore_errors=True)
    hidden_dir.mkdir(parents=True, exist_ok=True)
    (hidden_dir / "leak.json").write_text('{"secret_probe": true}', encoding="utf-8")
    try:
        assert _is_hidden_static_path("/static/.mimosa/reports/leak.json")
        assert _is_hidden_static_path("/static/.git/config")
        assert not _is_hidden_static_path("/static/app.js")
        assert not _is_hidden_static_path("/static/vendor/qrcode.min.js")
        with make_client(home, []) as client:
            assert client.get("/static/.mimosa/reports/leak.json").status_code == 404
            # 正常资源不受影响
            assert client.get("/static/app.js").status_code == 200
    finally:
        shutil.rmtree(STATIC_DIR / ".mimosa", ignore_errors=True)


# ---- 6. 客户端来源判定（保持既有行为不回归） ----


def test_client_is_local_untouched():
    """来源判定函数行为不变（环回=本机、局域网=远端）。"""
    assert _client_is_local(("127.0.0.1", 5000))
    assert not _client_is_local(("192.168.1.20", 5000))
