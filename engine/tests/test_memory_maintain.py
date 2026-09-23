"""定期自动整理记忆：全局 memory.md 与项目 AGENTS.md 的周期性合并去重。

巡检循环每 10 分钟一跳，测试里不等等——纯函数直接测，整合行为通过
app.state.backend 直接调 tick / _maintain_memory 验证（TestClient 的
with 块里 lifespan 已跑完；backend 这些方法不碰 store，可以安全地
asyncio.run 到测试自己的循环上）。
"""

from __future__ import annotations

import asyncio
import os

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.tools.memory import (
    MAINTAIN_MIN_GLOBAL_CHARS,
    MAINTENANCE_BACKUP_KEEP,
    backup_before_maintain,
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
        g_baks = list(mem_file.parent.glob("memory.md.bak-*"))
        assert len(g_baks) == 1 and g_baks[0].read_text(encoding="utf-8") == GLOBAL_OLD
        assert agents_md.read_text(encoding="utf-8").strip() == PROJECT_NEW
        p_baks = list(agents_md.parent.glob("AGENTS.md.bak-*"))
        assert len(p_baks) == 1 and p_baks[0].read_text(encoding="utf-8") == PROJECT_OLD

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
        assert asyncio.run(backend._maintain_memory("global", force=True)) == "skipped"
        assert len(provider.calls) == 0  # 阈值不足：模型都没调
        assert mem_file.read_text(encoding="utf-8") == "- [2026-09-01] 就一条"


def test_maintain_memory_unchanged_keeps_file(home, mem_file):
    """模型输出与原文一致（认为无需改动）→ 返回 unchanged，原文件与备份都不动。"""
    _seed_global(mem_file)
    provider = FakeProvider([[TextBlock(text=GLOBAL_OLD)]])
    with make_client(home, [], provider=provider) as client:
        backend = client.app.state.backend
        assert asyncio.run(backend._maintain_memory("global", force=True)) == "unchanged"
        assert mem_file.read_text(encoding="utf-8") == GLOBAL_OLD
        assert not list(mem_file.parent.glob("memory.md.bak-*"))  # 没落盘就没备份


def test_maintain_now_blocked_in_demo_mode(home, mem_file):
    """演示模式点「立即整理」必须拒绝：fake provider 的脚本文本不许盖掉真实记忆。"""
    _seed_global(mem_file)
    provider = FakeProvider([[TextBlock(text=GLOBAL_NEW)]])
    provider.demo_mode = True
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "memory.maintain_now"})
        res = recv_until(ws, "d1")
        assert res["ok"] is False and "演示模式" in res["error"]
        assert mem_file.read_text(encoding="utf-8") == GLOBAL_OLD  # 原件未被脚本文本覆盖
        assert len(provider.calls) == 0  # 模型都没调


def test_backup_before_maintain_rotates(mem_file):
    """整理备份带时间戳滚动保留：新备份写入，超出保留数的最旧被清掉。"""
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    for i in range(MAINTENANCE_BACKUP_KEEP + 2):
        backup_before_maintain(mem_file, f"第{i}版原件")
    baks = sorted(mem_file.parent.glob("memory.md.bak-*"))
    assert len(baks) == MAINTENANCE_BACKUP_KEEP
    assert baks[-1].read_text(encoding="utf-8") == f"第{MAINTENANCE_BACKUP_KEEP + 1}版原件"


def test_memory_save_mtime_guard(home, mem_file):
    """设置页保存带基线 mtime：编辑期间后台写过记忆就拒绝，重读后才能存。"""
    _seed_global(mem_file)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g1", "method": "memory.get"})
        r = recv_until(ws, "g1")["result"]
        assert r["mtime"] > 0 and r["inject_chars"] == len(r["text"])

        # 模拟后台（归档提炼）写入：文件内容与 mtime 都变了
        newer = "- [2026-09-21] (自动) 归档提炼的新条目"
        mem_file.write_text(GLOBAL_OLD + "\n" + newer, encoding="utf-8")
        os.utime(mem_file, (r["mtime"] - 10, r["mtime"] - 10))

        ws.send_json({"id": "s1", "method": "memory.save",
                      "params": {"text": GLOBAL_OLD, "base_mtime": r["mtime"]}})
        res = recv_until(ws, "s1")
        assert res["ok"] is False and "被更新过" in res["error"]
        assert newer in mem_file.read_text(encoding="utf-8")  # 后台新条目没有被盖掉

        # 重读拿新 mtime 后保存成功，返回新 mtime
        ws.send_json({"id": "g2", "method": "memory.get"})
        r2 = recv_until(ws, "g2")["result"]
        ws.send_json({"id": "s2", "method": "memory.save",
                      "params": {"text": GLOBAL_OLD + "\n" + newer + "\n- 手动新增",
                                 "base_mtime": r2["mtime"]}})
        res2 = recv_until(ws, "s2")["result"]
        assert res2["saved"] and res2["mtime"] > 0
        assert "- 手动新增" in mem_file.read_text(encoding="utf-8")


def test_project_instructions_mtime_guard(home):
    """右侧面板保存同理：编辑期间定期整理重写过 AGENTS.md 就拒绝，重读后可存。"""
    agents_md = home / "proj" / "AGENTS.md"
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    agents_md.write_text(PROJECT_OLD, encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "project.instructions"})
        r = recv_until(ws, "i1")["result"]
        assert r["mtime"] > 0 and "约定1" in r["text"]

        # 模拟后台（定期整理）重写：内容与 mtime 都变了
        agents_md.write_text(PROJECT_NEW, encoding="utf-8")
        os.utime(agents_md, (r["mtime"] - 10, r["mtime"] - 10))

        ws.send_json({"id": "s1", "method": "project.save_instructions",
                      "params": {"text": "用户编辑区的旧内容", "base_mtime": r["mtime"]}})
        res = recv_until(ws, "s1")
        assert res["ok"] is False and "重读" in res["error"]
        assert agents_md.read_text(encoding="utf-8") == PROJECT_NEW

        ws.send_json({"id": "i2", "method": "project.instructions"})
        r2 = recv_until(ws, "i2")["result"]
        ws.send_json({"id": "s2", "method": "project.save_instructions",
                      "params": {"text": PROJECT_NEW + "\n\n## 新约定\n\n- 用中文写提交信息",
                                 "base_mtime": r2["mtime"]}})
        assert recv_until(ws, "s2")["ok"] is True
