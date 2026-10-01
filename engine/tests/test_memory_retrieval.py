"""记忆检索化（第一期）：超长 memory.md 从「整块注入」变「按相关性选取注入」。

覆盖：切分与评分的纯函数单测（中英混排样例）、小记忆逐字节不变、
超阈值只注入相关子集并附说明行、0 条/全零分的整块兜底，
以及一条走真实 WS 轮首重组的集成用例。
"""

from __future__ import annotations

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.tools.memory import (
    MEMORY_RETRIEVAL_THRESHOLD,
    render_memory_section,
    select_relevant,
    split_memory_entries,
)


@pytest.fixture
def mem_file(home):
    # home 夹具返回 tmp_path，SKYSHEEP_HOME 指到 tmp_path/home/，记忆文件在其下
    p = home / "home" / "memory.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


MIXED_MEMORY = (
    "- [2026-08-01] 用户偏好 uv 管理 Python 依赖\n"
    "- [2026-09-01] 用户常用编辑器是 VS Code\n"
    "\n"
    "## 环境\n"
    "- 开发机是 Windows 11\n"
    "  终端是 PowerShell 7\n"
    "- 仓库根在 D:\\work\n"
    "\n"
    "部署一律走 GitHub Pages，静态站点不设后端。\n"
    "域名在 Cloudflare 托管。\n"
    "1. 第一条编号要点\n"
)


# ---------------------------------------------------------------- 切分


def test_split_memory_entries_mixed_shapes():
    """列表行逐条切、缩进续行归所属条目、标题自成条目、连续段落合一条。"""
    entries = split_memory_entries(MIXED_MEMORY)
    assert len(entries) == 7
    assert entries[0] == "- [2026-08-01] 用户偏好 uv 管理 Python 依赖"
    assert entries[1] == "- [2026-09-01] 用户常用编辑器是 VS Code"
    assert entries[2] == "## 环境"
    assert entries[3] == "- 开发机是 Windows 11\n  终端是 PowerShell 7"
    assert entries[4] == "- 仓库根在 D:\\work"
    assert entries[5] == (
        "部署一律走 GitHub Pages，静态站点不设后端。\n域名在 Cloudflare 托管。"
    )
    assert entries[6] == "1. 第一条编号要点"


def test_split_memory_entries_preserves_verbatim_text():
    """条目是原文的逐字截取：列表符号、日期前缀、缩进一律不动（选中后要原文注入）。"""
    for entry in split_memory_entries(MIXED_MEMORY):
        assert entry in MIXED_MEMORY


def test_split_memory_entries_blank_only_yields_zero():
    """切分出 0 条的情形：空串与纯空白。"""
    assert split_memory_entries("") == []
    assert split_memory_entries(" \n\n  \n") == []


# ---------------------------------------------------------------- 评分


def test_select_relevant_ranks_cjk_ascii_and_mixed():
    """中英混排查询都能命中：CJK 二元组、ASCII 词、同条目双语任一路都算分。"""
    entries = split_memory_entries(MIXED_MEMORY)
    # 纯中文查询命中 Python 依赖那条
    assert select_relevant(entries, "依赖管理用什么工具", limit=3)[0] == 0
    # 纯英文查询命中编辑器那条（大小写不敏感）；中英混合走编辑/辑器二元组同条目复核
    assert select_relevant(entries, "PowerShell", limit=3) == [3]
    assert select_relevant(entries, "VS Code 编辑器", limit=3)[0] == 1
    # 中英混合查询同时命中多条，按得分降序
    picked = select_relevant(entries, "部署到 Cloudflare 用什么", limit=3)
    assert picked[0] == 5  # 段落条目同时含「部署」「Cloudflare」
    assert select_relevant(entries, "PowerShell", limit=3) == [3]


def test_select_relevant_all_zero_returns_empty():
    """全部 0 分返回空列表——调用方据此兜底整块注入。"""
    entries = split_memory_entries(MIXED_MEMORY)
    assert select_relevant(entries, "量子纠缠薛定谔方程", limit=3) == []
    assert select_relevant(entries, "", limit=3) == []


def test_select_relevant_limit_and_index_tiebreak():
    """limit 截断；同分按原下标序，行为确定可测。"""
    entries = ["- 苹果 香蕉", "- 苹果", "- 香蕉 苹果 樱桃"]
    assert select_relevant(entries, "苹果", limit=2) == [0, 1]
    assert select_relevant(entries, "苹果", limit=0) == []
    assert select_relevant([], "苹果", limit=3) == []


# ---------------------------------------------------------------- 注入


def test_render_small_memory_byte_identical_with_and_without_query(mem_file):
    """≤ 阈值的记忆：带不带查询逐字节一致，且与检索化之前的格式一致、无说明行。"""
    raw = (
        "- [2026-09-01] 用户偏好 uv 管理 Python 依赖\n"
        "- [2026-09-02] 用户在 Windows 11 上开发\n"
    )
    assert len(raw) <= MEMORY_RETRIEVAL_THRESHOLD
    mem_file.write_text(raw, encoding="utf-8")
    no_query = render_memory_section()
    with_query = render_memory_section(query_text="Python 依赖管理怎么配")
    assert no_query == with_query
    assert with_query == (
        "\n# User memory（跨项目的用户记忆，管理用 memory_write）\n"
        + raw.strip()
        + "\n"
    )
    assert "按相关性注入" not in with_query


def _over_threshold_memory() -> str:
    lines = []
    for i in range(20):
        if i == 3:
            lines.append("- [2026-01-04] 用户偏好 uv 管理 Python 依赖")
        else:
            lines.append(f"- [2026-01-{i + 1:02d}] 无关记忆{i} " + "其他内容" * 20)
    raw = "\n".join(lines)
    assert len(raw) > MEMORY_RETRIEVAL_THRESHOLD
    return raw


def test_render_large_memory_injects_relevant_subset_with_note(mem_file):
    """超阈值 + 有查询：只注入相关子集（原文逐字），附一行说明。"""
    raw = _over_threshold_memory()
    mem_file.write_text(raw, encoding="utf-8")
    section = render_memory_section(query_text="帮我配一下 Python 的依赖管理")
    assert "已按相关性注入 1 条" in section
    assert "完整记忆在 memory.md" in section
    assert "- [2026-01-04] 用户偏好 uv 管理 Python 依赖" in section  # 原文注入
    assert "无关记忆" not in section  # 无关条目不占预算


def test_render_falls_back_to_whole_block_when_all_scores_zero(mem_file):
    """全部 0 分：整块兜底（宁多勿漏），不带检索说明行。"""
    raw = _over_threshold_memory()
    mem_file.write_text(raw, encoding="utf-8")
    section = render_memory_section(query_text="量子纠缠薛定谔方程")
    assert "按相关性注入" not in section
    assert "无关记忆0" in section and "无关记忆19" in section
    assert "用户偏好 uv 管理 Python 依赖" in section


def test_render_no_query_keeps_legacy_whole_block(mem_file):
    """拿不到查询（轮外 set_system 场景）：维持原来的整块注入，与老实现逐字节一致。"""
    raw = _over_threshold_memory()
    mem_file.write_text(raw, encoding="utf-8")
    section = render_memory_section()
    assert "按相关性注入" not in section
    assert "无关记忆0" in section and "用户偏好 uv 管理 Python 依赖" in section


def test_render_memory_missing_file(mem_file):
    assert render_memory_section() == ""
    assert render_memory_section(query_text="随便什么") == ""


# ---------------------------------------------------------------- WS 集成


def _chat_once(home, text):
    provider = FakeProvider([[TextBlock(text="好的")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        recv_until(ws, "n1")
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": text}})
        assert recv_until(ws, "c1")["result"]["done"]
    assert provider.calls, "模型未被调用"
    return provider.calls[0][0]  # 首个 system 消息


def test_turn_injects_relevant_subset_into_system(home, mem_file):
    """轮首重组生效：超阈值记忆在本轮只注入与用户消息相关的条目。"""
    mem_file.write_text(_over_threshold_memory(), encoding="utf-8")
    system = _chat_once(home, text="帮我配一下 Python 的依赖管理")
    assert system.role == "system"
    assert "已按相关性注入" in system.text
    assert "用户偏好 uv 管理 Python 依赖" in system.text
    assert "无关记忆" not in system.text


def test_turn_small_memory_system_unchanged(home, mem_file):
    """小记忆的轮首重组是同文本刷新：system 与直接渲染逐字节一致、无说明行。"""
    raw = "- [2026-09-01] 用户偏好 uv 管理 Python 依赖\n"
    mem_file.write_text(raw, encoding="utf-8")
    system = _chat_once(home, text="你好")
    assert system.role == "system"
    assert "按相关性注入" not in system.text
    # 组装顺序 base + 技能 + 项目约定 + 记忆段：记忆段是 system 的后缀，
    # 与直接渲染逐字节一致（轮首重组是同文本刷新）
    assert system.text.endswith(render_memory_section())
    assert system.text.endswith(raw.strip() + "\n")
