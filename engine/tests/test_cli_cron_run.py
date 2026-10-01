"""`skysheep cron run <task_id>`：CLI 冒烟（SKYSHEEP_HOME 隔离 + fake provider）。

覆盖：命令分发与退出码、结果回写任务行与消息落库、停用任务跳过、
失败回写、notify_channel 推送复用（假渠道）。绝不指向真实 ~/.skysheep。
"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.config import db_path
from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.session import SessionStore


@pytest.fixture
async def cron_task(home):
    """隔离库里的项目 + 定时任务（interval 30 分钟、启用、开关默认关）。"""
    store = await SessionStore(db_path()).connect()
    try:
        proj = await store.get_or_create_project(str(home / "proj"))
        task = await store.add_cron_task(
            proj.id, "每日汇总", "汇总进展", "interval", interval_minutes=30)
        yield task
    finally:
        await store.close()


def _patch_provider(monkeypatch, provider: FakeProvider) -> None:
    """把 cli.app 的 build_provider 换成假模型（隔离环境没有真实 API Key）。"""
    import skysheep.cli.app as cli_app

    monkeypatch.setattr(cli_app, "build_provider", lambda name, pc: provider)


def test_cron_run_cli_ok_writes_back_task_row(home, cron_task, monkeypatch):
    """正常路径：退出码 0，结果写回任务行，会话与消息落库，下次排期重算。"""
    import skysheep.cli.app as cli_app

    provider = FakeProvider([[TextBlock(text="今日汇总：完成三件事")]])
    _patch_provider(monkeypatch, provider)

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 0
    assert provider.calls, "fake provider 应被执行"

    async def check():
        store = await SessionStore(db_path()).connect()
        try:
            row = await store.get_cron_task(cron_task["id"])
            assert row["last_status"] == "ok"
            assert row["last_result"] == "今日汇总：完成三件事"
            assert row["next_run_at"] > 0, "运行后重算下次时间"
            sessions = await store.list_sessions(row["project_id"])
            assert any((s.title or "").startswith("⏰ ") for s in sessions), \
                "每个任务一个独立会话（⏰ 前缀，与后端同款）"
            for s in sessions:
                if (s.title or "").startswith("⏰ "):
                    msgs = await store.load_messages(s.id)
                    assert any(m.role == "assistant" and "完成三件事" in m.text
                               for m in msgs), "消息落库可审计"
        finally:
            await store.close()

    asyncio.run(check())


def test_cron_run_cli_skips_disabled_task(home, cron_task, monkeypatch):
    """停用的任务：尊重开关直接跳过，退出码 0，不产生任何模型调用。"""
    import skysheep.cli.app as cli_app

    provider = FakeProvider([[TextBlock(text="不该出现")]])
    _patch_provider(monkeypatch, provider)

    async def disable():
        store = await SessionStore(db_path()).connect()
        try:
            await store.update_cron_task(cron_task["id"], enabled=0)
        finally:
            await store.close()

    asyncio.run(disable())

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 0
    assert provider.calls == [], "停用任务不得执行"


def test_cron_run_cli_failure_writes_error_row(home, cron_task, monkeypatch):
    """模型调用异常：退出码 1，任务行回写 error（供应用内列表与推送消费）。"""
    import skysheep.cli.app as cli_app

    provider = FakeProvider([])
    _patch_provider(monkeypatch, provider)

    async def boom(*_a, **_k):
        raise RuntimeError("密钥无效")
        yield

    monkeypatch.setattr(provider, "stream", boom)

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 1

    async def check():
        store = await SessionStore(db_path()).connect()
        try:
            row = await store.get_cron_task(cron_task["id"])
            assert row["last_status"] == "error"
            assert "密钥无效" in (row["last_result"] or "")
        finally:
            await store.close()

    asyncio.run(check())


def test_cron_run_cli_pushes_summary_to_channel(home, cron_task, monkeypatch):
    """notify_channel 开 + 渠道启用且名单非空 → 终态摘要照常推送（逻辑复用）。"""
    from test_cron import _FakeChannel

    import skysheep.cli.app as cli_app

    class _Mgr:
        def __init__(self, channel):
            self.channels = {"feishu": channel}

    ch = _FakeChannel()

    async def enable():
        store = await SessionStore(db_path()).connect()
        try:
            await store.update_cron_task(cron_task["id"], notify_channel=1)
        finally:
            await store.close()

    asyncio.run(enable())
    monkeypatch.setattr(cli_app, "_build_sendonly_channels", lambda cfg: _Mgr(ch))
    _patch_provider(monkeypatch, FakeProvider([[TextBlock(text="推送我：完成")]]))
    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 0

    # 推送是 fire-and-forget（spawn_bg）：轮询等它落地
    import time as _time

    end = _time.time() + 5.0
    while _time.time() < end and not ch.sent:
        _time.sleep(0.02)
    assert ch.sent, "开了开关的定时任务应推送终态摘要"
    _chat_id, text = ch.sent[0]
    assert "每日汇总" in text and "成功" in text
