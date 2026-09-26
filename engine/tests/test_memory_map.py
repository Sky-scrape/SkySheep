"""记忆地图：项目演化聚合（map_*）、演化摘要生成与 map.* WS 协议。

覆盖三层：
- store：map_digests 表的读写替换与时间线三组聚合查询
- 纯函数：记忆条目解析、摘要 JSON 宽容解析、材料拼装与校验折算
- WS：map.get 载荷形状、map.generate（fake provider 走通/失败广播）、
  map.save_config 往返（全部走 make_client 的隔离 home，不碰真实 ~/.skysheep/）
"""

from __future__ import annotations

import json
import time

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.config import load_config, set_memory_map_config
from skysheep.core.checkpoints import CheckpointStore
from skysheep.messages import Message, TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend
from skysheep.tools.memory import parse_memory_entries

LONG_USER = "帮我重构权限门，把白名单判定按实际 shell 取。" * 4
LONG_REPLY = "已重构 security/gate.py 的 _has_shell_chain，Windows 走 cmd.exe 判拼接。" * 4
DIGEST_JSON = json.dumps({
    "overview": "项目从权限体系起步，逐步加固安全面",
    "phases": [
        {"title": "奠基", "summary": "搭好项目骨架并跑通测试",
         "highlights": ["建骨架", "跑通测试"], "topics": ["脚手架"], "session_ids": [1]},
        {"title": "加固", "summary": "重构权限门与白名单判定",
         "highlights": ["按 shell 判拼接"], "topics": ["安全"], "session_ids": [2]},
    ],
}, ensure_ascii=False)


# ---------------------------------------------------------------- store 聚合


async def test_map_digests_roundtrip_and_replace(store):
    p = await store.get_or_create_project("/tmp/mm-a")
    s1 = await store.create_session(p.id, "初始版本")
    s2 = await store.create_session(p.id, "权限门重构")
    await store.replace_map_digests(p.id, [
        {"kind": "overview", "title": "项目总览", "summary": "做了权限门",
         "session_ids": [s1.id, s2.id], "start_ts": 1, "end_ts": 2},
        {"kind": "phase", "title": "奠基", "summary": "初始版本", "highlights": ["建骨架"],
         "topics": ["脚手架"], "session_ids": [s1.id], "start_ts": 1, "end_ts": 2},
    ])
    ds = await store.list_map_digests(p.id)
    assert [d["kind"] for d in ds] == ["overview", "phase"]  # overview 排最前
    assert ds[1]["highlights"] == ["建骨架"] and ds[1]["topics"] == ["脚手架"]
    assert ds[1]["session_ids"] == [s1.id]
    # 整体替换：旧摘要整体作废，不留残行
    await store.replace_map_digests(p.id, [
        {"kind": "phase", "title": "二阶段", "session_ids": [s2.id]},
    ])
    ds2 = await store.list_map_digests(p.id)
    assert len(ds2) == 1 and ds2[0]["title"] == "二阶段"
    assert await store.latest_map_digest_ts(p.id) > 0
    # 别项目的摘要互不干扰
    other = await store.get_or_create_project("/tmp/mm-b")
    assert await store.list_map_digests(other.id) == []


async def test_map_sessions_stats_includes_archived_and_counts(store):
    p = await store.get_or_create_project("/tmp/mm-c")
    s1 = await store.create_session(p.id, "有消耗的会话")
    s2 = await store.create_session(p.id, "被归档的会话")
    await store.append_message(s1.id, Message.user("hello"))
    await store.add_usage(s1.id, "openai", "gpt", 100, 200, 0)
    await store.set_archived(s2.id, True)
    rows = await store.map_sessions_with_stats(p.id, 0, time.time() + 10)
    by_id = {r["id"]: r for r in rows}
    assert by_id[s1.id]["msg_count"] == 1 and by_id[s1.id]["in_tokens"] == 100
    assert "有消耗的会话" in {r["title"] for r in rows}
    assert by_id[s2.id]["archived"] is True  # 归档会话计入演化史
    assert by_id[s2.id]["msg_count"] == 0


async def test_map_day_stats_and_events(store):
    p = await store.get_or_create_project("/tmp/mm-d")
    s = await store.create_session(p.id, "会话")
    await store.add_usage(s.id, "openai", "gpt", 500, 500, 0)
    days = await store.map_day_stats(p.id, 0, time.time() + 10)
    assert len(days) == 1
    assert days[0]["sessions"] == 1 and days[0]["tokens"] == 1000
    t = await store.add_project_task(p.id, "做渠道")
    await store.update_project_task(t["id"], done=True)
    ev = await store.map_project_events(p.id, 0, time.time() + 10)
    kinds = [e["kind"] for e in ev]
    assert "task_created" in kinds and "task_done" in kinds
    assert ev == sorted(ev, key=lambda e: e["ts"])


async def test_map_count_since(store):
    p = await store.get_or_create_project("/tmp/mm-e")
    await store.create_session(p.id, "一")
    assert await store.count_project_sessions_since(p.id, 0) == 1
    assert await store.count_project_sessions_since(p.id, time.time() + 10) == 0


# ---------------------------------------------------------------- 纯函数


def test_parse_memory_entries_dates_and_limit():
    lines = [
        "# 说明行不算",
        "- [2026-08-01] (自动) 用户团队用 uv",
        "没有日期前缀的旧格式行不算",
    ]
    lines += [f"- [2026-09-0{i}] 条目{i}" for i in range(1, 10)]
    out = parse_memory_entries("\n".join(lines), limit=5)
    assert len(out) == 5
    assert out[0]["date"] == "2026-09-05"  # 取最近 5 条
    assert out[-1]["text"] == "条目9"


def test_map_digest_json_tolerates_fences_and_prose():
    raw = "好的，以下是总结：\n```json\n" + DIGEST_JSON + "\n```\n希望对你有帮助"
    data = ServerBackend._parse_map_digest_json(raw)
    assert data["phases"][0]["title"] == "奠基"
    with pytest.raises(ValueError):
        ServerBackend._parse_map_digest_json("这不是 JSON")


def test_map_digest_rows_maps_ids_and_dedupes():
    sessions = [
        {"id": "aaa", "title": "s1", "created_at": 10, "updated_at": 20,
         "msg_count": 1, "in_tokens": 0, "out_tokens": 0, "tags": [], "summary": ""},
        {"id": "bbb", "title": "s2", "created_at": 30, "updated_at": 40,
         "msg_count": 1, "in_tokens": 0, "out_tokens": 0, "tags": [], "summary": ""},
    ]
    data = json.loads(DIGEST_JSON)
    rows = ServerBackend._map_digest_rows(data, sessions)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["overview", "phase", "phase"]
    # 编号 → 真实会话 id，阶段时间窗取覆盖会话的极值
    assert rows[1]["session_ids"] == ["aaa"] and rows[1]["start_ts"] == 10
    assert rows[2]["end_ts"] == 40
    # 编号重叠/越界：先到先得 + 忽略，不产生幽灵阶段
    data2 = {"phases": [
        {"title": "A", "session_ids": [1, 2]},
        {"title": "B", "session_ids": [2, 3]},  # 2 已被 A 占用、3 越界
    ]}
    rows2 = ServerBackend._map_digest_rows(data2, sessions)
    assert [r["title"] for r in rows2] == ["A"]
    assert rows2[0]["session_ids"] == ["aaa", "bbb"]


def test_map_build_material_includes_numbers_and_summary():
    sessions = [{"id": "aaa", "title": "s1", "created_at": 0, "updated_at": 0,
                 "msg_count": 3, "in_tokens": 100, "out_tokens": 100,
                 "tags": ["重构"], "summary": "一句话摘要"}]
    prompt = ServerBackend._map_build_material("演示项目", sessions)
    assert "[1]" in prompt and "演示项目" in prompt
    assert "一句话摘要" in prompt and "重构" in prompt
    assert "不要 markdown 代码围栏" in prompt


def test_memory_map_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    assert load_config().memory_map.auto_digest is False  # 默认关
    set_memory_map_config(auto_digest=True)
    assert load_config().memory_map.auto_digest is True
    # 相邻配置节不受影响
    assert load_config().memory_maintenance.global_enabled is True


# ---------------------------------------------------------------- 检查点项目级扫描


def test_checkpoint_list_project_metas(tmp_path):
    root = tmp_path / "cps"
    st = CheckpointStore(root)
    st.save("sess-a", {"/tmp/f.py": b"old"})
    st.save("sess-a", {"/tmp/f.py": b"older"})
    st.save("sess-b", {"/tmp/g.md": None})  # 改前不存在的文件也是一条足迹
    metas = st.list_project_metas()
    assert len(metas) == 3
    assert [m["ts"] for m in metas] == sorted(m["ts"] for m in metas)  # 时间升序
    sids = {m["session_id"] for m in metas}
    assert sids == {"sess-a", "sess-b"}
    assert all("paths" in m for m in metas)


# ---------------------------------------------------------------- WS 协议


def _open_and_new_session(ws):
    ws.send_json({"id": "n1", "method": "session.new"})
    return recv_until(ws, "n1")["result"]["id"]


def test_map_get_shape_and_empty_state(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "map.get"})
        r = recv_until(ws, "m1")["result"]
        assert r["project"]["name"]
        assert r["sessions"] == [] and r["digests"] == []
        assert r["range"]["start_ts"] < r["range"]["end_ts"]
        assert r["config"]["auto_digest"] is False  # 默认关


def test_map_get_returns_sessions_after_chat(home):
    provider = FakeProvider([[TextBlock(text=LONG_REPLY)]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        sid = _open_and_new_session(ws)
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": LONG_USER}})
        assert recv_until(ws, "c1")["result"]["done"]
        ws.send_json({"id": "m1", "method": "map.get"})
        r = recv_until(ws, "m1")["result"]
        assert [s["id"] for s in r["sessions"]] == [sid]
        s = r["sessions"][0]
        assert s["msg_count"] == 2 and s["title"]  # 用户 + 助手
        assert r["days"] and r["days"][0]["tokens"] > 0  # 热力图有数


def test_map_generate_flow_and_event(home):
    # 脚本组：每轮 chat 各耗一组，第三组才是摘要输出
    provider = FakeProvider([
        [TextBlock(text=LONG_REPLY)],
        [TextBlock(text=LONG_REPLY)],
        [TextBlock(text="```json\n" + DIGEST_JSON + "\n```")],  # 带围栏的摘要输出
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        sid1 = _open_and_new_session(ws)
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": LONG_USER}})
        assert recv_until(ws, "c1")["result"]["done"]
        sid2 = _open_and_new_session(ws)
        ws.send_json({"id": "c2", "method": "chat.send", "params": {"text": LONG_USER}})
        assert recv_until(ws, "c2")["result"]["done"]

        ws.send_json({"id": "g1", "method": "map.generate"})
        assert recv_until(ws, "g1")["result"]["started"] is True

        for _ in range(60):
            frame = ws.receive_json()
            if frame.get("event") == "map_updated":
                assert frame["data"]["ok"] is True
                assert "阶段" in frame["data"]["message"]
                break
        else:
            pytest.fail("生成完成未收到 map_updated 事件")

        ws.send_json({"id": "m1", "method": "map.get"})
        r = recv_until(ws, "m1")["result"]
        kinds = [d["kind"] for d in r["digests"]]
        assert kinds == ["overview", "phase", "phase"]
        # 编号映射回真实会话 id；阶段时间窗落在会话时间上
        assert r["digests"][1]["session_ids"] in ([sid1], [sid2], [sid1, sid2])
        assert r["digests"][1]["topics"] == ["脚手架"]

        # 再次生成是「重总结」：单飞锁已放（上次已完成），旧摘要被整体替换
        provider.scripted.append([TextBlock(text=DIGEST_JSON)])
        ws.send_json({"id": "g2", "method": "map.generate"})
        assert recv_until(ws, "g2")["result"]["started"] is True
        for _ in range(60):
            frame = ws.receive_json()
            if frame.get("event") == "map_updated" and frame["data"].get("ok"):
                break
        else:
            pytest.fail("第二次生成未收到 map_updated 事件")
        ws.send_json({"id": "m2", "method": "map.get"})
        r2 = recv_until(ws, "m2")["result"]
        # 整体替换：仍是 总览 + 2 阶段，不残留旧行
        assert [d["kind"] for d in r2["digests"]] == ["overview", "phase", "phase"]


def test_map_generate_invalid_output_broadcasts_failure(home):
    provider = FakeProvider([
        [TextBlock(text=LONG_REPLY)],
        [TextBlock(text="我无法总结这段内容")],  # 没有 JSON → 解析失败
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        _open_and_new_session(ws)
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": LONG_USER}})
        assert recv_until(ws, "c1")["result"]["done"]

        ws.send_json({"id": "g1", "method": "map.generate"})
        assert recv_until(ws, "g1")["result"]["started"] is True
        for _ in range(60):
            frame = ws.receive_json()
            if frame.get("event") == "map_updated":
                assert frame["data"]["ok"] is False
                assert "失败" in frame["data"]["message"]
                break
        else:
            pytest.fail("解析失败未广播 map_updated")
        # 失败后单飞锁已放：map.get 不带任何摘要，且 generating 状态复位
        ws.send_json({"id": "m1", "method": "map.get"})
        r = recv_until(ws, "m1")["result"]
        assert r["digests"] == [] and r["generating"] is False


def test_map_generate_empty_project_broadcasts_guidance(home):
    """空项目点生成：任务照常启动（started=True），后台广播友好引导而非报错栈。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g1", "method": "map.generate"})
        assert recv_until(ws, "g1")["result"]["started"] is True
        for _ in range(60):
            frame = ws.receive_json()
            if frame.get("event") == "map_updated":
                assert frame["data"]["ok"] is False
                assert "还没有会话" in frame["data"]["message"]
                break
        else:
            pytest.fail("空项目生成未收到引导事件")
        # 单飞锁已放：map.get 的 generating 状态复位
        ws.send_json({"id": "m1", "method": "map.get"})
        assert recv_until(ws, "m1")["result"]["generating"] is False


def test_map_generate_requires_provider(home):
    """无模型服务时当场报错（在派发后台任务之前就拦下）。"""
    import asyncio

    with make_client(home, []) as client:
        backend = client.app.state.backend
        backend.provider = None
        with pytest.raises(RuntimeError, match="模型服务"):
            asyncio.new_event_loop().run_until_complete(backend.map_generate({}))


def test_map_save_config_roundtrip(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "map.save_config",
                      "params": {"auto_digest": True}})
        assert recv_until(ws, "s1")["result"]["auto_digest"] is True
        ws.send_json({"id": "m1", "method": "map.get"})
        assert recv_until(ws, "m1")["result"]["config"]["auto_digest"] is True


def test_map_auto_digest_tick_gating(home, monkeypatch):
    """自动巡检的触发判定：开关关/条件不满足不动手；满足即派发生成任务。

    只验证判定与派发，不真跑生成（完整流程在 flow 用例里覆盖）：store 计数
    打桩避免跨事件循环访问 aiosqlite；spawn_bg 打桩接住派发的协程。
    """
    import asyncio

    import skysheep.server.backend as backend_mod

    provider = FakeProvider([[TextBlock(text=LONG_REPLY)]]).with_default(
        [TextBlock(text=LONG_REPLY)])
    spawned = []

    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()  # 接住但不跑：生成流程另有用例

    monkeypatch.setattr(backend_mod, "spawn_bg", fake_spawn)

    with make_client(home, [], provider=provider) as client:
        backend = client.app.state.backend

        async def fake_latest(pid):
            return 0.0

        async def fake_count(pid, ts):
            return ServerBackend.MAP_AUTO_MIN_SESSIONS  # 刚好达标

        backend.store.latest_map_digest_ts = fake_latest
        backend.store.count_project_sessions_since = fake_count

        # 开关默认关：不触发
        asyncio.run(backend._map_auto_digest_tick())
        assert spawned == []

        # 开关打开但冷却期未过（一分钟前刚生成过）：不触发
        backend.cfg = type(backend.cfg).model_validate(
            {**backend.cfg.model_dump(), "memory_map": {"auto_digest": True}})

        async def fake_recent(pid):
            return time.time() - 60

        backend.store.latest_map_digest_ts = fake_recent
        asyncio.run(backend._map_auto_digest_tick())
        assert spawned == []

        # 最近生成是 25h 前 + 新会话达标 → 触发一次
        async def fake_stale(pid):
            return time.time() - (ServerBackend.MAP_AUTO_COOLDOWN_S + 3600)

        backend.store.latest_map_digest_ts = fake_stale
        asyncio.run(backend._map_auto_digest_tick())
        assert len(spawned) == 1

        # 新会话不达标：不触发
        async def fake_few(pid, ts):
            return 1

        backend.store.count_project_sessions_since = fake_few
        asyncio.run(backend._map_auto_digest_tick())
        assert len(spawned) == 1
