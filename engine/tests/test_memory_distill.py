"""轮次自动沉淀（记忆二期）：候选制抽取、去重、待审列表持久化、采纳/忽略。

纯函数与状态文件直接测；MemoryMixin 的轮次服务（schedule_turn_distill /
_turn_distill）用直连 ServerBackend + FakeProvider 验证（spawn_bg 需要
在跑的事件循环，asyncio 测试自带）。开关默认关、失败静默、不打扰。
"""

from __future__ import annotations

import json
import time

import pytest

from skysheep.messages import Message, TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend
from skysheep.tools import memory_distill
from skysheep.tools.memory import remember_lines
from skysheep.tools.memory_distill import (
    CANDIDATE_MAX_CHARS,
    DISTILL_STATE_FILE,
    add_candidates,
    adopt_candidate,
    build_distill_prompt,
    distill_enabled,
    distill_state_path,
    filter_new_candidates,
    ignore_candidate,
    list_candidates,
    load_distill_state,
    parse_distill_output,
    set_distill_enabled,
)

LONG_USER = "帮我规划这个 Python 项目的依赖管理，我们团队一直用 uv 管理虚拟环境，" * 4
LONG_ASSISTANT = "好的，建议把 uv 的锁定文件提交进仓库，CI 里统一用 uv sync 安装依赖。" * 4
DISTILL_REPLY = "- 用户团队用 uv 管理 Python 依赖\n- 用户项目都放在 D 盘"


@pytest.fixture
def state_file(home):
    # home 夹具已把 SKYSHEEP_HOME 指到 tmp/home/，状态文件在其下
    return home / "home" / DISTILL_STATE_FILE


@pytest.fixture
def mem_file(home):
    p = home / "home" / "memory.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


async def _mk_backend(home, prov) -> ServerBackend:
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: prov)
    await be.setup()
    return be


# ---------------------------------------------------------------- 提示词与解析


def test_build_distill_prompt_structured():
    """结构化中文提示词：限 3 条、宁缺毋滥、禁任务细节与机密。"""
    p = build_distill_prompt("用户：……\n助手：……")
    assert "最多 3 条" in p
    assert "候选" in p and "宁缺毋滥" in p
    assert "机密" in p and "API Key" in p
    assert "对话记录" in p


def test_parse_distill_output_caps_three_and_filters():
    raw = (
        "好的，以下是候选：\n"
        "- 候选一\n"
        "1. 候选二\n"
        "无。\n"
        "- 候选三\n"
        "- 候选四\n"
        "- 候选五\n"
    )
    out = parse_distill_output(raw)
    assert out == ["候选一", "候选二", "候选三"]  # 占位行/前导语滤掉 + 最多 3 条
    assert parse_distill_output("无") == []
    long = "长" * 300
    out = parse_distill_output(f"- {long}")
    assert len(out) == 1 and out[0].endswith("…") and len(out[0]) <= CANDIDATE_MAX_CHARS + 1


# ---------------------------------------------------------------- 去重与状态文件


def test_filter_new_candidates_dedups_three_ways(mem_file):
    """三方去重：memory.md 既有条目、待审列表、已忽略名单；剥空白后子串互比。"""
    mem_file.write_text(
        "- [2026-09-01] (自动) 用户团队用 uv 管理 Python 依赖\n", encoding="utf-8"
    )
    state = {
        "enabled": True,
        "pending": [{"id": "x", "text": "用户项目都放在 D 盘"}],
        "ignored": ["用户喜欢简洁的回复"],
    }
    fresh = filter_new_candidates(
        [
            "用户团队用 uv 管理 Python 依赖",  # 与 memory.md 既有条目相同
            "用户团队用 uv",                    # 既有条目的子串
            "用 uv 管理 Python 依赖",           # 候选更精炼：仍是既有条目的子串
            "用户项目都放在 D 盘",               # 与待审列表重复
            "用户喜欢简洁的",                    # 与已忽略互为子串
            "用户常用 VS Code 编辑器",           # 新候选
        ],
        mem_file.read_text(encoding="utf-8"),
        state,
    )
    assert fresh == ["用户常用 VS Code 编辑器"]
    # 批内重复只留一条
    assert filter_new_candidates(["新事实", "新事实"], "", state) == ["新事实"]


def test_state_roundtrip_default_off_and_corrupt(state_file):
    """开关默认关；状态文件损坏/非对象按出厂态处理；写回是合法 JSON。"""
    assert not state_file.exists()
    assert distill_state_path() == state_file  # 落点在 SKYSHEEP_HOME（测试已隔离）
    assert distill_enabled() is False
    assert load_distill_state() == {"enabled": False, "pending": [], "ignored": []}
    assert set_distill_enabled(True) is True
    assert distill_enabled() is True  # 立即可读（热生效）
    assert json.loads(state_file.read_text(encoding="utf-8"))["enabled"] is True
    state_file.write_text("不是 JSON{{{", encoding="utf-8")
    assert distill_enabled() is False  # 损坏按出厂态
    state_file.write_text("[1,2]", encoding="utf-8")
    assert load_distill_state() == {"enabled": False, "pending": [], "ignored": []}


def test_add_candidates_persists_with_context_and_time(home, state_file, mem_file, monkeypatch):
    """新候选带 id/上下文摘录/时间落盘；重复候选不再进；封顶丢最旧。"""
    mem_file.write_text("", encoding="utf-8")
    added = add_candidates(
        ["用户团队用 uv 管理 Python 依赖"],
        context="对话摘录……" * 100,
        session_id="s1",
        memory_text="",
    )
    assert len(added) == 1
    ent = added[0]
    assert ent["id"] and ent["session_id"] == "s1" and ent["ts"] > 0
    assert len(ent["context"]) <= 200  # 上下文摘录限长
    stored = list_candidates()
    assert [e["id"] for e in stored] == [ent["id"]]
    assert time.time() - stored[0]["ts"] < 60

    # 同一候选再来一轮（文本或子串）不再进待审
    assert add_candidates(["用户团队用 uv"], memory_text="") == []
    assert len(list_candidates()) == 1

    # 封顶：超出 PENDING_MAX 丢最旧（monkeypatch 到 3 好造满员场景）
    monkeypatch.setattr(memory_distill, "PENDING_MAX", 3)
    add_candidates(["甲", "乙", "丙", "丁"], memory_text="")
    texts = [e["text"] for e in list_candidates()]
    assert len(texts) == 3 and "乙" in texts and "丁" in texts
    assert "用户团队用 uv 管理 Python 依赖" not in texts  # 最旧的被挤出


# ---------------------------------------------------------------- 采纳 / 忽略


def test_adopt_moves_candidate_into_memory_md(mem_file, state_file):
    """采纳：候选经 remember_lines 进 memory.md（日期 + (自动) 标记），移出待审。"""
    added = add_candidates(["用户团队用 uv 管理 Python 依赖"], memory_text="")
    cid = added[0]["id"]
    res = adopt_candidate(cid)
    assert res["adopted"] is True
    assert res["added"] == ["用户团队用 uv 管理 Python 依赖"]
    text = mem_file.read_text(encoding="utf-8")
    assert "- [" in text and "(自动) 用户团队用 uv 管理 Python 依赖" in text
    assert list_candidates() == []  # 待审列表已清

    # 已采纳的内容再被提议 → 去重挡住（这次读得到真实 memory.md）
    assert add_candidates(["用户团队用 uv 管理 Python 依赖"]) == []

    # 重复采纳 / 不存在的 id：adopted=False 且不抛错
    assert adopt_candidate(cid)["adopted"] is False
    assert adopt_candidate("no-such-id")["adopted"] is False


def test_adopt_dedupes_against_memory_and_drops_entry(mem_file, state_file):
    """记忆里已有相同内容时采纳不重复追加，但候选仍要移出待审。"""
    remember_lines(["用户团队用 uv 管理 Python 依赖"])
    # 进待审时 memory.md 还没有这条（memory_text="" 显式跳过文件比对）
    added = add_candidates(["用户团队用 uv 管理 Python 依赖"], memory_text="")
    assert len(added) == 1
    # 采纳时读真实 memory.md：已有相同内容 → 不重复追加，但移出待审
    before = mem_file.read_text(encoding="utf-8")
    res = adopt_candidate(added[0]["id"])
    assert res["adopted"] is False and res["reason"] == "记忆里已有相同内容"
    assert mem_file.read_text(encoding="utf-8") == before
    assert list_candidates() == []


def test_ignore_drops_and_remembers(mem_file, state_file):
    """忽略：候选丢弃 + 记入已忽略名单，下一轮同一事实不再进待审。"""
    added = add_candidates(["用户喜欢深色主题"], memory_text="")
    cid = added[0]["id"]
    res = ignore_candidate(cid)
    assert res["ignored"] is True
    assert list_candidates() == []
    assert load_distill_state()["ignored"] == ["用户喜欢深色主题"]
    assert not mem_file.exists()  # 忽略绝不写 memory.md

    # 下一轮同一事实（含子串变体）被再次提出 → 已忽略名单挡住
    assert add_candidates(["用户喜欢深色主题"], memory_text="") == []
    assert add_candidates(["用户喜欢深色"], memory_text="") == []
    assert ignore_candidate("no-such-id")["ignored"] is False


# ---------------------------------------------------------------- 轮次服务（MemoryMixin）


def _turn_msgs():
    return [
        Message.user(LONG_USER),
        Message.assistant([TextBlock(text=LONG_ASSISTANT)]),
    ]


async def test_turn_distill_extracts_candidates(home, state_file, mem_file):
    """轮次收尾抽取：走 provider、候选带上下文与时间进待审列表，不动 memory.md。"""
    prov = FakeProvider([[TextBlock(text=DISTILL_REPLY)]])
    be = await _mk_backend(home, prov)
    try:
        await be._turn_distill("sid-1", _turn_msgs())

        assert len(prov.calls) == 1
        assert "对话记录" in prov.calls[0][0].text  # 喂给模型的是提炼稿
        pending = list_candidates()
        assert [e["text"] for e in pending] == [
            "用户团队用 uv 管理 Python 依赖", "用户项目都放在 D 盘",
        ]
        assert all(e["session_id"] == "sid-1" and e["context"] and e["ts"] for e in pending)
        assert not mem_file.exists()  # 候选制：不写 memory.md、不打扰
        # 直调 _turn_distill 没经过 schedule（单飞标记在那里惰性建）：有则必须已释放
        assert getattr(be, "_distilling", set()) == set()
    finally:
        await be.shutdown()


async def test_turn_distill_skips_short_turns_and_failures(home, state_file, mem_file):
    """过短轮次不调模型；无候选不写状态；provider 异常静默不上抛。"""
    prov = FakeProvider([[TextBlock(text="好的")]])
    be = await _mk_backend(home, prov)
    be2 = None
    be3 = None
    try:
        await be._turn_distill(
            "sid-1", [Message.user("你好"), Message.assistant([TextBlock(text="好的")])]
        )
        assert prov.calls == []  # 寒暄轮连模型都不调
        assert not state_file.exists()

        be2 = await _mk_backend(home, FakeProvider([[TextBlock(text="无")]]))
        await be2._turn_distill("sid-2", _turn_msgs())
        assert len(be2.provider.calls) == 1  # 调了模型
        assert not state_file.exists()  # 没有候选：待审列表不动

        class _Boom:
            async def stream(self, messages, tool_schemas, effort=None):
                raise RuntimeError("网络炸了")
                yield  # pragma: no cover（使本函数成为异步生成器）

        be3 = await _mk_backend(home, FakeProvider([]))
        be3.provider = _Boom()
        await be3._turn_distill("sid-3", _turn_msgs())  # 异常被吞，不上抛
        assert not state_file.exists()
    finally:
        await be.shutdown()
        if be2 is not None:
            await be2.shutdown()
        if be3 is not None:
            await be3.shutdown()


async def test_schedule_turn_distill_gating(home, state_file, monkeypatch):
    """挂点门控：开关默认关不触发；演示模式不触发；开启后 spawn_bg 派发、按会话单飞。"""
    import skysheep.server.backend_parts.memory as mem_mod

    spawned: list = []
    monkeypatch.setattr(
        mem_mod, "spawn_bg", lambda coro: spawned.append(coro)  # 测试随后自行 await
    )
    prov = FakeProvider([[TextBlock(text=DISTILL_REPLY)]])
    be = await _mk_backend(home, prov)
    try:
        # 默认关：什么都不发生
        be.schedule_turn_distill("sid-1", _turn_msgs())
        assert spawned == [] and prov.calls == []

        # 演示模式：开了也不触发（不消耗脚本组）
        set_distill_enabled(True)
        prov.demo_mode = True
        be.schedule_turn_distill("sid-1", _turn_msgs())
        assert spawned == []
        prov.demo_mode = False

        # 开启：spawn_bg 派发，单飞标记挂上；同会话进行中再派发 → 让路
        be.schedule_turn_distill("sid-1", _turn_msgs())
        assert len(spawned) == 1
        assert "sid-1" in be._distilling
        be.schedule_turn_distill("sid-1", _turn_msgs())
        assert len(spawned) == 1

        # 无事件循环环境（RuntimeError）→ 标记回滚，不悬挂
        def _no_loop(coro):
            coro.close()
            raise RuntimeError("no running loop")

        monkeypatch.setattr(mem_mod, "spawn_bg", _no_loop)
        be.schedule_turn_distill("sid-2", _turn_msgs())
        assert "sid-2" not in be._distilling

        # 派发的协程照常跑完：候选落待审、标记释放
        await spawned[0]
        assert [e["text"] for e in list_candidates()] == [
            "用户团队用 uv 管理 Python 依赖", "用户项目都放在 D 盘",
        ]
        assert be._distilling == set()
    finally:
        await be.shutdown()


async def test_memory_candidate_wrappers(home, state_file, mem_file):
    """接线面：开关读写、候选列表、采纳刷新系统提示词、忽略走包装器。"""
    be = await _mk_backend(home, FakeProvider([[TextBlock(text=DISTILL_REPLY)]]))
    try:
        assert (await be.memory_distill_save(True))["enabled"] is True
        assert await be.memory_candidates() == {"enabled": True, "pending": []}

        await be._turn_distill("sid-1", _turn_msgs())
        pending = (await be.memory_candidates())["pending"]
        assert len(pending) == 2

        res = await be.memory_candidate_adopt(pending[0]["id"])
        assert res["adopted"] is True
        assert "用户团队用 uv 管理 Python 依赖" in be.compose_system()  # 采纳后立刻生效
        res = await be.memory_candidate_ignore(pending[1]["id"])
        assert res["ignored"] is True
        assert (await be.memory_candidates())["pending"] == []
    finally:
        await be.shutdown()
