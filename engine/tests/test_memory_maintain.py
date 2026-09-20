"""定期自动整理记忆：全局 memory.md 与项目 AGENTS.md 的周期性合并去重。

巡检循环每 10 分钟一跳，测试里不等等——纯函数直接测，整合行为通过
app.state.backend 直接调 tick / _maintain_memory 验证（TestClient 的
with 块里 lifespan 已跑完；backend 这些方法不碰 store，可以安全地
asyncio.run 到测试自己的循环上）。
"""

from __future__ import annotations

import asyncio

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.tools.memory import (
    MAINTAIN_MIN_GLOBAL_CHARS,
    build_maintain_prompt,
    clean_maintained_text,
    load_maintenance_state,
    maintenance_due,
    maintenance_state_path,
    save_maintenance_state,
)

GLOBAL_OLD = "\n".join(f"- [2026-09-{d:02d}] 用户偏好条目{d}，喜欢简洁直接的回复" for d in range(1, 16))
GLOBAL_NEW = "- [2026-09-01] 用户偏好：喜欢简洁直接的回复"
_PROJECT_LINE = "提交信息用中文，先跑测试再提交；改动过宽要先说明影响范围，等确认后再动手。"
PROJECT_OLD = "\n\n".join(f"## 约定{i}\n\n- {_PROJECT_LINE}" for i in range(1, 16))
PROJECT_NEW = f"## 约定\n\n- {_PROJECT_LINE}"


@pytest.fixture
def mem_file(home):
    """全局记忆文件路径（conftest 的 home 夹具已把 SKYSHEEP_HOME 指到 home/home）。"""
    return home / "home" / "memory.md"


# ---------------------------------------------------------------- 纯函数


def test_maintenance_state_roundtrip(home):
    assert load_maintenance_state() == {}
    save_maintenance_state({"global_last": 123.5, "project_last": {"D:\\x": 9.0}})
    st = load_maintenance_state()
    assert st["global_last"] == 123.5 and st["project_last"]["D:\\x"] == 9.0
    # 损坏文件按空处理
    maintenance_state_path().write_text("not json", encoding="utf-8")
    assert load_maintenance_state() == {}


def test_maintenance_due():
    now = 1_000_000.0
    st = {"global_last": now - 100 * 3600, "project_last": {"/p": now - 1 * 3600}}
    assert maintenance_due(st, global_enabled=True, project_enabled=True,
                           interval_hours=72, workdir="/p", now=now) == (True, False)
    # 开关关闭 / 从未整理过
    assert maintenance_due(st, global_enabled=False, project_enabled=True,
                           interval_hours=72, workdir="/p", now=now) == (False, False)
    assert maintenance_due({}, global_enabled=True, project_enabled=True,
                           interval_hours=72, workdir="/p", now=now) == (True, True)


def test_clean_maintained_text():
    old = "原始内容"
    assert clean_maintained_text("  整理后  \n", old, 1000) == "整理后"
    assert clean_maintained_text("```markdown\n整理后\n```\n", old, 1000) == "整理后"
    assert clean_maintained_text("", old, 1000) is None  # 空
    assert clean_maintained_text(" 原始内容 ", old, 1000) is None  # 没变化
    assert clean_maintained_text("x" * 1001, old, 1000) is None  # 超长失控
    assert "不要任何解释" in build_maintain_prompt("global", old)
    assert "AGENTS.md" in build_maintain_prompt("project", old)


# ---------------------------------------------------------------- 整合行为


def _seed_global(mem_file):
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    mem_file.write_text(GLOBAL_OLD, encoding="utf-8")
    assert len(GLOBAL_OLD) >= MAINTAIN_MIN_GLOBAL_CHARS


def test_maintain_now_global_and_project(home, mem_file):
    _seed_global(mem_file)
    agents_md = home / "proj" / "AGENTS.md"
    agents_md.write_text(PROJECT_OLD, encoding="utf-8")
    provider = FakeProvider([
        [TextBlock(text=f"```markdown\n{GLOBAL_NEW}\n```")],   # 全局整理输出（带围栏）
        [TextBlock(text=PROJECT_NEW)],                          # 项目整理输出
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "memory.maintain_now"})
        res = recv_until(ws, "m1")["result"]
        assert res["ran"] is True and res["global"] and res["project"]

        assert mem_file.read_text(encoding="utf-8").strip() == GLOBAL_NEW  # 围栏已剥
        assert mem_file.with_name("memory.md.bak").read_text(encoding="utf-8") == GLOBAL_OLD
        assert agents_md.read_text(encoding="utf-8").strip() == PROJECT_NEW
        assert agents_md.with_name("AGENTS.md.bak").read_text(encoding="utf-8") == PROJECT_OLD

        st = load_maintenance_state()
        assert st["global_last"] > 0 and st["project_last"][str(home / "proj")] > 0

        # 系统提示词已带上整理后的记忆
        backend = client.app.state.backend
        assert "用户偏好" in backend.compose_system()

        # 整理后的两份文件都低于阈值 → 立刻再跑：两份都跳过（ran=False，不花模型调用）
        calls = len(provider.calls)
        ws.send_json({"id": "m2", "method": "memory.maintain_now"})
        res2 = recv_until(ws, "m2")["result"]
        assert res2["ran"] is False and "还没到需要整理的规模" in res2["message"]
        assert len(provider.calls) == calls
        assert mem_file.read_text(encoding="utf-8").strip() == GLOBAL_NEW


def test_maintain_tick_respects_toggles(home, mem_file):
    """巡检 tick 按开关与周期决定动不动手；整理过的短时间内不再重复。"""
    _seed_global(mem_file)
    provider = FakeProvider([[TextBlock(text=GLOBAL_NEW)]])
    with make_client(home, [], provider=provider) as client:
        backend = client.app.state.backend
        # 从未整理过 + 周期任意 → tick 立即到期，整理全局
        asyncio.run(backend._memory_maintenance_tick())
        assert mem_file.read_text(encoding="utf-8").strip() == GLOBAL_NEW

        # 周期拉满再 tick：不到期，不再调模型
        calls = len(provider.calls)
        asyncio.run(backend.memory_maintain_save(interval_hours=8760))
        asyncio.run(backend._memory_maintenance_tick())
        assert len(provider.calls) == calls

        # 关掉全局开关、重置时间为从未整理 → tick 也不动手
        asyncio.run(backend.memory_maintain_save(global_enabled=False))
        save_maintenance_state({})
        asyncio.run(backend._memory_maintenance_tick())
        assert len(provider.calls) == calls


def test_maintain_settings_ws_roundtrip(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g1", "method": "memory.get"})
        m = recv_until(ws, "g1")["result"]["maintain"]
        assert m["global_enabled"] is True and m["project_enabled"] is True
        assert m["interval_hours"] == 168 and m["global_last"] == 0

        ws.send_json({"id": "s1", "method": "memory.maintain_save",
                      "params": {"global_enabled": False, "interval_hours": 24}})
        res = recv_until(ws, "s1")["result"]
        assert res == {"global_enabled": False, "project_enabled": True, "interval_hours": 24}

        # 落盘持久：重新读回（backend 保存时已 reload config）
        ws.send_json({"id": "g2", "method": "memory.get"})
        m2 = recv_until(ws, "g2")["result"]["maintain"]
        assert m2["global_enabled"] is False and m2["interval_hours"] == 24

        # 非法周期报错
        ws.send_json({"id": "s2", "method": "memory.maintain_save",
                      "params": {"interval_hours": 0}})
        assert recv_until(ws, "s2")["ok"] is False


def test_maintain_too_short_skips(home, mem_file):
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    mem_file.write_text("- [2026-09-01] 就一条", encoding="utf-8")
    provider = FakeProvider([[TextBlock(text=GLOBAL_NEW)]])
    with make_client(home, [], provider=provider) as client:
        backend = client.app.state.backend
        assert asyncio.run(backend._maintain_memory("global", force=True)) is False
        assert len(provider.calls) == 0  # 阈值不足：模型都没调
        assert mem_file.read_text(encoding="utf-8") == "- [2026-09-01] 就一条"
