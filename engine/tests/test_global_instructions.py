"""全局自定义指令：~/.skysheep/instructions.md 的读写与系统提示词注入。"""

from __future__ import annotations

from test_server import make_client, recv_until

from skysheep.core.prompt import (
    global_instructions_path,
    load_global_instructions,
    render_global_instructions_section,
)


def test_global_instructions_file_roundtrip(home):
    p = global_instructions_path()
    assert p.parent == home / "home"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("- 回答先给结论\n- 不要自动提交 git", encoding="utf-8")

    text = load_global_instructions()
    assert "回答先给结论" in text
    section = render_global_instructions_section(text)
    assert "# Global user instructions" in section
    assert "不要自动提交 git" in section


def test_global_instructions_missing_file_is_empty(home):
    assert load_global_instructions() == ""
    assert render_global_instructions_section("") == ""


def test_instructions_ws_roundtrip(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "instructions.save",
                      "params": {"text": "- 每次改完代码都跑测试"}})
        saved = recv_until(ws, "i1")
        assert saved["ok"] is True
        assert saved["result"]["chars"] > 0
        ws.send_json({"id": "i2", "method": "instructions.get", "params": {}})
        got = recv_until(ws, "i2")
        assert got["ok"] is True
        assert "跑测试" in got["result"]["text"]
        assert got["result"]["max_chars"] == 8000


def test_instructions_save_rejects_stale_mtime(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "instructions.save",
                      "params": {"text": "- v1"}})
        assert recv_until(ws, "i1")["ok"] is True
        # 模拟另一个窗口改过文件：mtime 与编辑时不一致
        p = global_instructions_path()
        p.write_text("- 别的窗口写入", encoding="utf-8")
        ws.send_json({"id": "i2", "method": "instructions.save",
                      "params": {"text": "- 我的旧编辑", "base_mtime": 1.0}})
        stale = recv_until(ws, "i2")
        assert stale["ok"] is False
        assert "其他窗口" in stale["error"] or "改过" in stale["error"]
        assert "别的窗口写入" in load_global_instructions(), "冲突时不得覆盖新内容"
