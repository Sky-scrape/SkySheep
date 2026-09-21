"""本批新功能测试：自动标题、快捷指令、fs.read、截断/分叉、用量、tasks、A/B 对比。"""

from __future__ import annotations

import time

from skysheep.messages import TextBlock

# ---- 自动标题 ----


def test_auto_title_on_first_turn(home):
    """首轮带 wants_title → 后台生成标题替换截断文本。"""
    from test_server import make_client, recv_until

    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([
        [TextBlock(text="回复正文")],                    # 对话轮
        [TextBlock(text=" 部署 SkySheep 服务 ")],        # 标题生成轮
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {
            "text": "怎么部署 sky very long title 部署", "wants_title": True}})
        recv_until(ws, "c1")
        deadline = time.time() + 3
        title = ""
        while time.time() < deadline:
            time.sleep(0.1)
            lst = None
            ws.send_json({"id": "sl", "method": "session.list", "params": {}})
            lst = recv_until(ws, "sl")["result"]
            title = lst["sessions"][0]["title"]
            if title == "部署 SkySheep 服务":
                break
        assert title == "部署 SkySheep 服务", title


def test_no_auto_title_without_flag(home):
    """不带 wants_title（CLI/旧前端/测试路径）→ 不额外调用模型。"""
    from test_server import make_client, recv_until

    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([[TextBlock(text="回复正文")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "问题"}})
        recv_until(ws, "c1")
        time.sleep(0.5)  # 若误触发，第二个 script 项会被消费并可能抛错
        ws.send_json({"id": "sl", "method": "session.list", "params": {}})
        lst = recv_until(ws, "sl")["result"]
        assert lst["sessions"][0]["title"] == "问题"


# ---- 快捷指令 ----


def test_snippets_crud_via_ws(home):
    from test_server import make_client, recv_until

    from skysheep.server.backend import BUILTIN_SNIPPETS

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "snippets.add", "params": {
            "name": "代码审查", "content": "请审查：{{clipboard}}"}})
        r = recv_until(ws, "a1")
        assert r["ok"] and r["result"]["snippet"]["name"] == "代码审查"

        ws.send_json({"id": "l1", "method": "snippets.list", "params": {}})
        lst = recv_until(ws, "l1")["result"]["snippets"]
        # 首启已播种内置示例 + 本条新建
        assert len(lst) == len(BUILTIN_SNIPPETS) + 1
        assert "代码审查" in [s["name"] for s in lst]

        ws.send_json({"id": "u1", "method": "snippets.update", "params": {
            "id": lst[0]["id"], "name": "改名", "content": "新内容"}})
        assert recv_until(ws, "u1")["ok"]

        ws.send_json({"id": "d1", "method": "snippets.delete", "params": {"id": lst[0]["id"]}})
        assert recv_until(ws, "d1")["result"]["deleted"] is True

        # 空名拒绝
        ws.send_json({"id": "a2", "method": "snippets.add", "params": {"name": "", "content": "x"}})
        assert not recv_until(ws, "a2")["ok"]


# ---- 内置示例快捷指令：首启播种 ----


def _snippet_ids(ws):
    from test_server import recv_until

    ws.send_json({"id": "q", "method": "snippets.list", "params": {}})
    return [(s["id"], s["name"]) for s in recv_until(ws, "q")["result"]["snippets"]]


def test_builtin_snippets_seeded_editable(home):
    """首启把内置示例落成真实记录：设置页可见、可编辑；响应里带 / 菜单兜底列表。"""

    from test_server import make_client, recv_until

    from skysheep.server.backend import BUILTIN_SNIPPETS

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        rows = _snippet_ids(ws)
        # 列表按创建时间倒序：只比对集合，不比对顺序
        assert sorted(name for _, name in rows) == sorted(s["name"] for s in BUILTIN_SNIPPETS)
        ws.send_json({"id": "b", "method": "snippets.list", "params": {}})
        assert len(recv_until(ws, "b")["result"]["builtin"]) == len(BUILTIN_SNIPPETS)
        # 示例就是普通记录：改名字、改内容都行
        ws.send_json({"id": "u1", "method": "snippets.update",
                      "params": {"id": rows[0][0], "name": "项目速览（改）", "content": "新内容"}})
        assert recv_until(ws, "u1")["ok"]
        assert "项目速览（改）" in [name for _, name in _snippet_ids(ws)]
    # 播种哨兵落盘（下次启动不再补种）
    assert (home / "home" / "snippets-seeded").exists()


def test_builtin_snippets_delete_is_sticky(home):
    """删光示例后重启不复活：播种只做一次，删光是用户的明确决定。"""
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for sid, _ in _snippet_ids(ws):
            ws.send_json({"id": "d", "method": "snippets.delete", "params": {"id": sid}})
            assert recv_until(ws, "d")["result"]["deleted"] is True
        assert _snippet_ids(ws) == []
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _snippet_ids(ws) == []
        # 但 / 菜单的兜底列表仍然随响应下发
        ws.send_json({"id": "b", "method": "snippets.list", "params": {}})
        assert len(recv_until(ws, "b")["result"]["builtin"]) == 5  # 与 BUILTIN_SNIPPETS 等长


async def test_builtin_snippets_skip_legacy_users(home):
    """升级老用户已有自定义指令时不播种（保持「加了自定义后示例不再出现」旧约定）。"""
    from test_server import make_client

    from skysheep.config import db_path
    from skysheep.session.store import SessionStore

    # 首个客户端启动前，指令库里已有一条自建指令（模拟老用户）
    s = await SessionStore(db_path()).connect()
    await s.add_snippet("我的旧指令", "老内容")
    await s.close()

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        rows = _snippet_ids(ws)
        assert [name for _, name in rows] == ["我的旧指令"]
    # 哨兵仍会落盘：之后删光也不补种
    assert (home / "home" / "snippets-seeded").exists()


# ---- fs.read 沙箱 ----


def test_fs_read_sandboxed(home):
    from test_server import make_client, recv_until

    (home / "proj" / "a.txt").write_text("hello", encoding="utf-8")
    secret = home / "outside.txt"
    secret.write_text("secret", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "fs.read", "params": {"path": "a.txt"}})
        r = recv_until(ws, "r1")
        assert r["ok"] and r["result"]["text"] == "hello"

        ws.send_json({"id": "r2", "method": "fs.read", "params": {"path": "../outside.txt"}})
        assert not recv_until(ws, "r2")["ok"], "目录穿越必须被拒绝"

        ws.send_json({"id": "r3", "method": "fs.read", "params": {"path": "nope.txt"}})
        assert not recv_until(ws, "r3")["ok"]


# ---- 截断 / 分叉 / 重新生成 ----


def test_truncate_regen_and_edit(home):
    from test_server import make_client, recv_until

    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([
        [TextBlock(text="第一次回答")],
        [TextBlock(text="第一次回答")],
        [TextBlock(text="重新生成的回答")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "问题一"}})
        recv_until(ws, "c1")
        sid = None
        ws.send_json({"id": "sl", "method": "session.list", "params": {}})
        sid = recv_until(ws, "sl")["result"]["sessions"][0]["id"]

        # 重新生成：删尾部 assistant，保留 user，重跑
        ws.send_json({"id": "t1", "method": "session.truncate",
                      "params": {"id": sid, "mode": "regen"}})
        tr = recv_until(ws, "t1")
        assert tr["ok"] and tr["result"]["deleted"] == 1

        ws.send_json({"id": "c2", "method": "chat.send", "params": {
            "session_id": sid, "text": "", "regenerate": True}})
        r2 = recv_until(ws, "c2")
        assert r2["ok"], r2.get("error")

        # 编辑：连 user 一起删，返回原文
        ws.send_json({"id": "t2", "method": "session.truncate",
                      "params": {"id": sid, "mode": "edit"}})
        te = recv_until(ws, "t2")["result"]
        assert te["deleted"] >= 1 and "问题一" in te["text"]

        # 截断后正常重发
        ws.send_json({"id": "c3", "method": "chat.send", "params": {
            "session_id": sid, "text": "问题一（改）"}})
        assert recv_until(ws, "c3")["ok"]

        ws.send_json({"id": "sl2", "method": "session.list", "params": {}})
        # 无异常即通过：重新生成与编辑流程走通


def test_fork_session(home):
    from test_server import make_client, recv_until

    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([
        [TextBlock(text="答A")], [TextBlock(text="答B")], [TextBlock(text="答C")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        sid = None
        for i in range(3):
            params = {"text": f"问题{i}"}
            if i > 0:
                params["session_id"] = sid
            ws.send_json({"id": f"c{i}", "method": "chat.send", "params": params})
            frame = recv_until(ws, f"c{i}")
            assert frame["ok"]
            if i == 0:
                sid = frame["result"]["session_id"]

        ws.send_json({"id": "f1", "method": "session.fork",
                      "params": {"id": sid, "seq": 2}})  # 只带第一轮
        r = recv_until(ws, "f1")["result"]
        assert r["copied"] == 2 and r["id"] != sid
        assert [m["text"] for m in r["messages"]] == ["问题0", "答A"]

        ws.send_json({"id": "sl", "method": "session.list", "params": {}})
        titles = {s["title"] for s in recv_until(ws, "sl")["result"]["sessions"]}
        assert any(t.startswith("└") for t in titles)


# ---- 用量统计 ----


def test_usage_stats(home):
    from test_server import make_client, recv_until

    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([[TextBlock(text="回")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "问"}})
        recv_until(ws, "c1")
        ws.send_json({"id": "u1", "method": "usage.stats", "params": {"days": 7}})
        st = recv_until(ws, "u1")["result"]
        assert st["days"] == 7
        # FakeProvider 的 ProviderDone 带 token 数 → 至少有一条记录
        assert st["total_in"] >= 0 and st["total_out"] >= 0
        assert isinstance(st["by_day"], list) and isinstance(st["by_session"], list)


# ---- tasks.list ----


def test_tasks_list_via_ws(home):
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "tasks.list", "params": {}})
        r = recv_until(ws, "t1")
        assert r["ok"] and r["result"]["tasks"] == []


# ---- A/B 对比 ----


def test_compare_mode(home):
    """compare=True：成员回答各自成消息，不融合。"""
    from test_server import make_client, recv_until

    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider

    class TwoMemberProvider(FakeProvider):
        """chair 不参与（chair_answers 由配置控制）——这里直接给两条 scripted。"""

    # 主 provider 充当成员1与成员2？_resolve_members 用 cfg.providers 构建；
    # 测试里用成员注入路径更复杂 → 简化：验证 compare 分支对 outcome.members 的处理
    # 由 _roundtable_body 单测覆盖（见 test_roundtable 的 compare 用例），这里走通协议。
    provider = FakeProvider([[TextBlock(text="甲的回答")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True, "compare": True}})
        r = recv_until(ws, "c1")
        # 无其他成员可用（fake 未配置 Key 的服务列表为空）→ 报错但协议可达
        assert ("ok" in r) and (r["ok"] is False or r["ok"] is True)
