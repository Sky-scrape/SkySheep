"""归档自动记忆：会话归档后后台提炼持久事实，写入 ~/.skysheep/memory.md。"""

from __future__ import annotations

import time

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.messages import Message, TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.tools.memory import (
    MAX_MEMORY_CHARS,
    build_digest_prompt,
    digest_transcript,
    load_memory_text,
    parse_digest,
    remember_lines,
)

LONG_USER = "帮我规划这个 Python 项目的依赖管理，我们团队一直用 uv 管理虚拟环境，" * 4
LONG_ASSISTANT = "好的，建议把 uv 的锁定文件提交进仓库，CI 里统一用 uv sync 安装依赖。" * 4
DIGEST_REPLY = "- 用户团队用 uv 管理 Python 依赖\n- 用户项目都放在 D 盘"


@pytest.fixture
def mem_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    return tmp_path / "home" / "memory.md"


# ---------------------------------------------------------------- 纯函数


def test_parse_digest_strips_and_filters():
    raw = "- 用户用 uv 管理依赖\n1. 团队项目在 D 盘\n2、喜欢中文回复\n\n* 又一条要点\n无"
    assert parse_digest(raw) == ["用户用 uv 管理依赖", "团队项目在 D 盘", "喜欢中文回复", "又一条要点"]


def test_parse_digest_filters_placeholder_punctuation_and_preambles():
    """「无。」带句尾标点的占位、「以下是提炼结果：」类前导语都不能混进记忆。"""
    raw = (
        "好的，以下是提炼结果：\n"
        "- 用户团队用 uv 管理 Python 依赖\n"
        "无。\n"
        "没有。\n"
        "（无）\n"
        "none.\n"
        "没有值得记录的内容！\n"
        "无需保存：\n"
        "1. 用户项目都放在 D 盘\n"
    )
    assert parse_digest(raw) == ["用户团队用 uv 管理 Python 依赖", "用户项目都放在 D 盘"]


def test_load_memory_text_truncates_at_line_boundary(mem_file):
    """注入超上限保最新的尾部条目、按行截断：半条记忆对模型是噪声，最后一条必须完整。"""
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    lines = ["- [2026-08-01] 最早的一条"] + ["- 条目" + "x" * 90 for _ in range(60)]
    lines.append("- [2026-09-24] 最新的一条")
    mem_file.write_text("\n".join(lines), encoding="utf-8")
    text = load_memory_text()
    assert len(text) <= MAX_MEMORY_CHARS
    assert "最新的一条" in text  # 保尾：越新的记忆越可能仍然有效，必须注入
    assert "最早的一条" not in text  # 丢头：最旧的让位（与容量护栏丢最旧同方向）
    assert all(ln.startswith("-") for ln in text.splitlines())  # 截在行边界，无半条


def test_render_memory_section_marks_truncation(mem_file):
    """发生截断时段落里必须说明「以下只是最近条目」，让模型知道更早的可 list 查看。"""
    from skysheep.tools.memory import render_memory_section

    assert render_memory_section() == ""  # 没有记忆文件
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    mem_file.write_text("- [2026-09-01] 就一条", encoding="utf-8")
    short = render_memory_section()
    assert "就一条" in short and "注入上限" not in short
    mem_file.write_text("\n".join("- 条目" + "x" * 90 for _ in range(60)), encoding="utf-8")
    truncated = render_memory_section()
    assert "注入上限" in truncated and "list" in truncated
    assert "最近" in truncated and "- 条目" in truncated  # 提示之外注入的仍是记忆本体


def test_parse_digest_caps_entries_and_length():
    assert len(parse_digest("\n".join(f"- 条目{i}" for i in range(20)))) == 8
    out = parse_digest("- " + "长" * 300)
    assert len(out) == 1 and out[0].endswith("…") and len(out[0]) <= 101


def test_remember_lines_dedups_and_prefixes_date(mem_file):
    assert remember_lines(["用户用 uv 管理依赖"]) == ["用户用 uv 管理依赖"]
    text = mem_file.read_text(encoding="utf-8")
    assert text.startswith("- [") and text.rstrip().endswith("用户用 uv 管理依赖")
    # 完全相同、以及「是现有条目子串」的候选都不重复记
    assert remember_lines(["用户用 uv 管理依赖", "用户用 uv"]) == []
    assert mem_file.read_text(encoding="utf-8") == text


def test_digest_transcript_filters_roles_and_threshold():
    short = [Message.user("你好"), Message.assistant([TextBlock(text="好的")])]
    assert digest_transcript(short) == ""
    msgs = [
        Message.system("系统提示词不算会话正文"),
        Message.user(LONG_USER),
        Message.tool_result("t1", "工具结果也不算"),
        Message.assistant([TextBlock(text=LONG_ASSISTANT)]),
    ]
    t = digest_transcript(msgs)
    assert t.startswith("用户：")
    assert "助手：" in t and "系统提示词" not in t and "工具结果" not in t
    assert "下面是一段" in build_digest_prompt(t)
    assert "机密" in build_digest_prompt(t)


# ---------------------------------------------------------------- WS 集成


def wait_memory(mem_file, want=True):
    deadline = time.time() + 10
    while time.time() < deadline:
        if mem_file.exists() == want:
            return True
        time.sleep(0.1)
    return False


def test_archive_triggers_memory_digest(home, mem_file):
    # 不传 wants_title（默认关）绕开自动标题的后台调用，脚本组时序完全确定：
    # 第 0 组 = 正式回复，第 1 组 = 归档提炼输出
    provider = FakeProvider([
        [TextBlock(text=LONG_ASSISTANT)],
        [TextBlock(text=DIGEST_REPLY)],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]

        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": LONG_USER}})
        assert recv_until(ws, "c1")["result"]["done"]
        assert len(provider.calls) == 1  # 只有正式回复这一轮调用

        ws.send_json({"id": "a1", "method": "session.archive",
                      "params": {"id": sid, "archived": True}})
        assert recv_until(ws, "a1")["result"]["archived"] is True

        assert wait_memory(mem_file), "归档后未写入记忆文件"
        text = mem_file.read_text(encoding="utf-8")
        assert "用户团队用 uv 管理 Python 依赖" in text
        assert "用户项目都放在 D 盘" in text
        # 自动条目带来源标记，用户在全局记忆页能一眼分辨并清理
        assert "(自动)" in text

        # 提炼完成的事件通知（前端 addNotice 用）
        for _ in range(15):
            frame = ws.receive_json()
            if frame.get("event") == "memory_digest":
                assert frame["data"]["added"] == 2
                assert "用户团队用 uv" in frame["data"]["message"]
                break
        else:
            pytest.fail("归档提炼完成未收到 memory_digest 事件")

        # 重复归档不再提炼（已归档 → 归档不是状态迁移）；取消归档也不触发；
        # 取消归档后再归档同样不触发（同会话只提炼一次，不重复花模型调用）
        calls = len(provider.calls)
        ws.send_json({"id": "a2", "method": "session.archive",
                      "params": {"id": sid, "archived": True}})
        recv_until(ws, "a2")
        ws.send_json({"id": "a3", "method": "session.archive",
                      "params": {"id": sid, "archived": False}})
        recv_until(ws, "a3")
        ws.send_json({"id": "a4", "method": "session.archive",
                      "params": {"id": sid, "archived": True}})
        recv_until(ws, "a4")
        time.sleep(0.8)
        assert len(provider.calls) == calls
        assert mem_file.read_text(encoding="utf-8") == text


def test_archive_short_session_skips_digest(home, mem_file):
    provider = FakeProvider([[TextBlock(text="好的")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]

        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "你好"}})
        assert recv_until(ws, "c1")["result"]["done"]
        calls = len(provider.calls)  # 只有正式回复这一轮调用（未开自动标题）

        ws.send_json({"id": "a1", "method": "session.archive",
                      "params": {"id": sid, "archived": True}})
        assert recv_until(ws, "a1")["ok"]
        time.sleep(1.0)

        assert len(provider.calls) == calls, "短会话不应触发提炼调用"
        assert not mem_file.exists()


def test_memory_digest_toggle_gates_archive(home, mem_file):
    """设置 · 全局记忆的总闸：关闭后归档不再提炼，重开恢复；状态持久化。"""
    provider = FakeProvider([
        [TextBlock(text=LONG_ASSISTANT)],   # 会话 1 正式回复
        [TextBlock(text=DIGEST_REPLY)],     # 会话 1 归档提炼
    ]).with_default([TextBlock(text=LONG_ASSISTANT)])  # 之后的会话都回长文本
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g0", "method": "memory.get"})
        assert recv_until(ws, "g0")["result"]["digest_enabled"] is True  # 默认开

        ws.send_json({"id": "n1", "method": "session.new"})
        sid1 = recv_until(ws, "n1")["result"]["id"]
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": LONG_USER}})
        assert recv_until(ws, "c1")["result"]["done"]
        ws.send_json({"id": "a1", "method": "session.archive",
                      "params": {"id": sid1, "archived": True}})
        recv_until(ws, "a1")
        assert wait_memory(mem_file), "开关默认开：归档应正常提炼"

        ws.send_json({"id": "g1", "method": "memory.digest_save",
                      "params": {"enabled": False}})
        assert recv_until(ws, "g1")["result"]["enabled"] is False
        ws.send_json({"id": "g2", "method": "memory.get"})
        assert recv_until(ws, "g2")["result"]["digest_enabled"] is False  # 已持久化

        text_before = mem_file.read_text(encoding="utf-8")
        ws.send_json({"id": "n2", "method": "session.new"})
        sid2 = recv_until(ws, "n2")["result"]["id"]
        ws.send_json({"id": "c2", "method": "chat.send", "params": {"text": LONG_USER}})
        assert recv_until(ws, "c2")["result"]["done"]
        calls = len(provider.calls)  # chat1 + digest1 + chat2
        ws.send_json({"id": "a2", "method": "session.archive",
                      "params": {"id": sid2, "archived": True}})
        assert recv_until(ws, "a2")["ok"]
        time.sleep(1.0)

        assert len(provider.calls) == calls, "开关关闭：归档不应触发提炼调用"
        assert mem_file.read_text(encoding="utf-8") == text_before

        ws.send_json({"id": "g3", "method": "memory.digest_save",
                      "params": {"enabled": True}})
        assert recv_until(ws, "g3")["result"]["enabled"] is True
