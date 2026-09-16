"""工作项目切换测试：应用内设定工作目录（project.switch）。"""

from __future__ import annotations

from test_server import make_client, recv_until  # helpers（home fixture 在 conftest.py）

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider


def test_switch_project_moves_engine_to_new_dir(home):
    # 新项目目录：带自己的 AGENTS.md 和一个文件
    proj2 = home / "proj2"
    (proj2 / "src").mkdir(parents=True)
    (proj2 / "AGENTS.md").write_text("proj2 的约定：全部用中文回复", encoding="utf-8")
    (proj2 / "src" / "app.py").write_text("print('proj2')", encoding="utf-8")
    # 旧项目留一个文件，切换后不应出现在 fs.files
    (home / "proj" / "old.txt").write_text("old", encoding="utf-8")

    provider = FakeProvider([])
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        # 切换
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        r = recv_until(ws, "sw")
        assert r["ok"] and r["result"]["switched"]
        assert r["result"]["path"] == str(proj2.resolve())

        # 快照反映新项目
        ws.send_json({"id": "b", "method": "boot", "params": {}})
        snap = recv_until(ws, "b")["result"]
        assert snap["working_dir"] == str(proj2.resolve())
        assert snap["instructions_file"].endswith("AGENTS.md")

        # 文件索引只见新项目
        ws.send_json({"id": "f", "method": "fs.files", "params": {}})
        files = recv_until(ws, "f")["result"]
        assert "src/app.py" in files["files"]
        assert not any(p.endswith("old.txt") for p in files["files"])

        # 项目列表：两个项目，当前是 proj2
        ws.send_json({"id": "pl", "method": "project.list", "params": {}})
        listing = recv_until(ws, "pl")["result"]
        current = [p for p in listing["projects"] if p["is_current"]]
        assert len(current) == 1 and current[0]["root_path"] == str(proj2.resolve())

        # 再次切换到同目录 → 幂等 no-op
        ws.send_json({"id": "sw2", "method": "project.switch", "params": {"path": str(proj2)}})
        r2 = recv_until(ws, "sw2")
        assert r2["ok"] and not r2["result"]["switched"]


def test_switch_project_then_tools_write_into_new_dir(home):
    proj2 = home / "proj2"
    proj2.mkdir()
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "made.txt", "content": "hi"})],
            [TextBlock(text="done")],
        ]
    )
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw")["ok"]

        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        while True:
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
                break
        assert fr["ok"]
        assert (proj2 / "made.txt").read_text(encoding="utf-8") == "hi"
        assert not (home / "proj" / "made.txt").exists()


def test_switch_project_rejects_bad_path_and_running_task(home):
    proj2 = home / "proj2"
    proj2.mkdir()
    provider = FakeProvider(
        [
            [ToolUseBlock(id="t1", name="write_file", input={"path": "b.txt", "content": "x"})],
            [TextBlock(text="done")],
        ]
    )
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        # 不存在的目录
        ws.send_json(
            {"id": "bad", "method": "project.switch", "params": {"path": str(home / "nope")}}
        )
        r = recv_until(ws, "bad")
        assert not r["ok"] and "目录不存在" in r["error"]

        # 相对路径
        ws.send_json({"id": "rel", "method": "project.switch", "params": {"path": "proj2"}})
        assert not recv_until(ws, "rel")["ok"]

        # 任务运行中（权限挂起 = 未结束）拒绝切换
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        perm = None
        for _ in range(200):
            fr = ws.receive_json()
            if "event" in fr and fr["event"] == "permission_request":
                perm = fr["data"]["request_id"]
                break
        assert perm
        ws.send_json({"id": "busy", "method": "project.switch", "params": {"path": str(proj2)}})
        rb = recv_until(ws, "busy")
        assert not rb["ok"] and "正在运行" in rb["error"]

        # 放行让任务收尾，再切换应该成功
        ws.send_json(
            {
                "id": "pa",
                "method": "permission.respond",
                "params": {"request_id": perm, "decision": "allow_once"},
            }
        )
        done = recv_until(ws, "c1")
        assert done["ok"]
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw")["ok"]
