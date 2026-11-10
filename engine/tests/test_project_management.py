"""项目软移除 / 恢复 / 管理列表 + 提示词斜杠别名（WS 入口）。"""

from __future__ import annotations

from test_server import make_client, recv_until


def call(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": method, "params": params or {}})
        return recv_until(ws, "r1")


def test_project_remove_restore_ws(home):
    admin = call(home, "project.admin_list")
    assert admin["ok"] is True
    projects = admin["result"]["projects"]
    assert projects, "home 夹具的 proj 应已登记为项目"
    p = projects[0]

    removed = call(home, "project.remove", {"id": p["id"]})
    assert removed["ok"] is True
    assert removed["result"]["was_current"] is True
    # 唯一项目被移除：进入无项目态，不删会话
    assert removed["result"]["switched_to"] is None
    listed = call(home, "project.list")
    assert all(x["id"] != p["id"] for x in listed["result"]["projects"])
    admin2 = call(home, "project.admin_list")
    assert any(x["id"] == p["id"] for x in admin2["result"]["removed"])

    restored = call(home, "project.restore", {"id": p["id"]})
    assert restored["ok"] is True
    listed2 = call(home, "project.list")
    assert any(x["id"] == p["id"] for x in listed2["result"]["projects"])
    admin3 = call(home, "project.admin_list")
    assert all(x["id"] != p["id"] for x in admin3["result"]["removed"])


def test_snippet_slash_alias_ws(home):
    added = call(home, "snippets.add", {
        "name": "代码审查", "content": "请审查以下代码", "slash": "review",
    })
    assert added["ok"] is True
    assert added["result"]["snippet"]["slash"] == "review"

    dup = call(home, "snippets.add", {
        "name": "另一个", "content": "x", "slash": "review",
    })
    assert dup["ok"] is False
    assert "占用" in dup["error"]

    reserved = call(home, "snippets.add", {
        "name": "占内置", "content": "x", "slash": "help",
    })
    assert reserved["ok"] is False
    assert "内置命令" in reserved["error"]

    bad = call(home, "snippets.add", {
        "name": "非法", "content": "x", "slash": "有中文",
    })
    assert bad["ok"] is False
    assert "字母" in bad["error"]

    sid = added["result"]["snippet"]["id"]
    updated = call(home, "snippets.update", {
        "id": sid, "name": "代码审查", "content": "改一下", "slash": "",
    })
    assert updated["ok"] is True
    got = call(home, "snippets.list")
    row = next(s for s in got["result"]["snippets"] if s["id"] == sid)
    assert row["slash"] == ""
