"""`skysheep cron run <task_id>`：CLI 冒烟（SKYSHEEP_HOME 隔离 + fake provider）。

覆盖：命令分发与退出码、结果回写任务行与消息落库、停用任务跳过、
失败回写、notify_channel 推送复用（假渠道）。绝不指向真实 ~/.skysheep。
"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.config import db_path
from skysheep.messages import TextBlock, ToolUseBlock
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
    """把 cli.app 的 build_provider 换成假模型（隔离环境没有真实 API Key）。

    签名带 *a/**k：cron run 的真实调用点现在把 cfg.network 作为第三 positional
    传入（无人值守也走全局代理/CA，F5.2），桩函数要接得住。
    """
    import skysheep.cli.app as cli_app

    monkeypatch.setattr(cli_app, "build_provider", lambda name, pc, *a, **k: provider)


def test_cron_run_cli_passes_network_to_build_provider(home, cron_task, monkeypatch):
    """F5.2：无人值守的模型流量也走全局出网设置——cron run 的 build_provider
    必须收到 cfg.network（代理 / no_proxy / 自签 CA），漏传会静默绕过直连。"""
    import skysheep.cli.app as cli_app
    from skysheep.config import load_config

    provider = FakeProvider([[TextBlock(text="今日汇总：完成")]])
    seen: dict = {}


    def _fake_build(name, pc, network=None):
        seen["name"] = name
        seen["network"] = network
        return provider

    monkeypatch.setattr(cli_app, "build_provider", _fake_build)

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 0
    from skysheep.network import NetworkConfig

    # load_config() 每次返回新实例，比对象值不比身份：收到的必须是配置里那份
    # NetworkConfig（同一进程内 cron run 与 load_config 同源）
    assert isinstance(seen["network"], NetworkConfig)
    assert seen["network"] == load_config().network, \
        "cron run 的 build_provider 必须拿到 cfg.network（与 run_headless / REPL 同口径）"


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


# 定时团队二期（团队模式设计 §14.6）：CLI 系统计划任务入口与后端扫描触发共用
# run_cron_team_turn_core。脚本池按「主模型 → 成员 → 总管」顺序供给（成员解析
# 与总管构建各建一颗独立 provider，均经 cli_app.build_provider 桩）。

_DIRECTOR_CLOSED_LOOP = [
    [TextBlock(text="分工方案：调研先行"), ToolUseBlock(
        id="d1", name="team_assign",
        input={"title": "巡检目录", "assignee": "队员0", "type": "exec"})],
    [TextBlock(text="对照验收标准：合格"), ToolUseBlock(
        id="d2", name="team_accept", input={"task_id": "T1"})],
    [TextBlock(text="验收通过，全部工单完成")],
    [TextBlock(text="《交付说明》：巡检完成。"), ToolUseBlock(
        id="d3", name="team_deliver", input={})],
]


def _patch_provider_pool(monkeypatch, scripts: list) -> None:
    """按序弹池的 build_provider 桩：主模型、成员、总管各拿各的脚本。"""
    import skysheep.cli.app as cli_app

    pool = [p if isinstance(p, FakeProvider) else FakeProvider(p) for p in scripts]

    def _fake_build(name, pc, *a, **k):
        if pool:
            return pool.pop(0)
        return FakeProvider([[TextBlock(text="脚本耗尽的兜底回答")]])

    monkeypatch.setattr(cli_app, "build_provider", _fake_build)


def _seed_team_template(name="夜班小队"):
    """往（隔离 home 的）模板册里塞一个定时团队引用的 AI 总管模板。"""
    from skysheep.config import skysheep_home
    from skysheep.core.team_store import (
        TeamTemplate,
        TeamTemplateDirector,
        TeamTemplateMember,
        TeamTemplateStore,
    )

    st = TeamTemplateStore(skysheep_home() / "teams.json")
    st.upsert(TeamTemplate(
        name=name, director_mode="ai",
        director=TeamTemplateDirector(provider="zhipu", model="ma"),
        members=[TeamTemplateMember(provider="zhipu", model="ma", name="队员0",
                                    persona="")],
        created_at=1.0,
    ))
    return name


async def _make_team_task(home, name="夜间巡检", prompt="巡检目录并汇总",
                          team_template="夜班小队"):
    store = await SessionStore(db_path()).connect()
    try:
        proj = await store.get_or_create_project(str(home / "proj"))
        return await store.add_cron_task(
            proj.id, name, prompt, "interval", interval_minutes=30,
            mode="team", team_template=team_template)
    finally:
        await store.close()


def test_cron_run_cli_team_task_runs_shared_core(home, monkeypatch):
    """二期口径：团队任务在系统计划任务入口与后端同链执行——模板建队、AI 总管
    闭环交付，run_cron_task_core 收尾照常（last_status=ok、《交付说明》进
    last_result、会话 user+assistant 落库、报告全文落盘 cron-team-*.md、用量
    入账）。一期的显式拒绝（「团队任务暂不支持系统计划任务入口」）已移除。"""
    import skysheep.cli.app as cli_app

    _patch_provider_pool(monkeypatch, [
        [TextBlock(text="主模型不参与团队轮")],
        [TextBlock(text="巡检完成：未见异常")],
        _DIRECTOR_CLOSED_LOOP,
    ])
    _seed_team_template("夜班小队")
    task = asyncio.run(_make_team_task(home))

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(task["id"])])
    assert ei.value.code == 0

    async def check():
        store = await SessionStore(db_path()).connect()
        try:
            row = await store.get_cron_task(task["id"])
            assert row["last_status"] == "ok"
            assert "《交付说明》" in (row["last_result"] or ""), "交付说明进 last_result"
            proj = await store.get_project(row["project_id"])
            sessions = await store.list_sessions(proj.id)
            sid = next(s.id for s in sessions if (s.title or "").startswith("⏰ "))
            msgs = await store.load_messages(sid)
            assert any(m.role == "user" and "巡检目录并汇总" in m.text for m in msgs)
            assert any(m.role == "assistant" and "《交付说明》" in m.text for m in msgs)
            assert await store.usage_today() > 0, "成员与总管用量入账归属本次会话"
        finally:
            await store.close()

    asyncio.run(check())

    reports = list((home / "proj" / ".skysheep" / "reports").glob("cron-team-*.md"))
    assert reports, "交付报告必须落盘一份（cron-team- 前缀）"
    text = reports[0].read_text(encoding="utf-8")
    assert "《交付说明》" in text and "夜班小队" in text and "终态" in text


def test_cron_run_cli_team_task_missing_template_fails(home, monkeypatch):
    """模板悬空引用在触发期按失败收尾（§14.5）：exit 1、任务行回写 error，
    只有主模型 provider 被装配（成员/总管构建未发生，不建注定空转的团队）。"""
    import skysheep.cli.app as cli_app

    member = FakeProvider([[TextBlock(text="不该被调用")]])
    _patch_provider_pool(monkeypatch, [
        [TextBlock(text="主模型不参与团队轮")],
        member,
    ])
    task = asyncio.run(_make_team_task(home, team_template="不存在的模板"))

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(task["id"])])
    assert ei.value.code == 1
    assert member.calls == [], "模板缺失时成员构建不得发生（不产生成员调用）"

    async def check():
        store = await SessionStore(db_path()).connect()
        try:
            row = await store.get_cron_task(task["id"])
            assert row["last_status"] == "error"
            assert "团队模板已不存在" in (row["last_result"] or "")
        finally:
            await store.close()

    asyncio.run(check())


def test_cron_run_cli_waits_for_inflight_push_before_exit(home, cron_task, monkeypatch):
    """退出前有界等在途推送落地：慢推送不再被 asyncio.run 收尾取消而静默丢失。

    终态推送是 fire-and-forget（spawn_bg），而本进程是无人值守推送的唯一
    出口：不等落地，asyncio.run 收尾会统一取消在途任务——CancelledError
    是 BaseException，渠道层 except Exception 接不住，bg 收尾对取消又不记
    日志，真实网络推送必丢且无痕。修复后 main() 返回前推送已落地（本用例
    不做任何退出后轮询）。
    """
    from test_cron import _FakeChannel

    import skysheep.cli.app as cli_app

    class _SlowChannel(_FakeChannel):
        async def send_text(self, chat_id, text):
            await asyncio.sleep(0.2)  # 模拟真实 DNS+TLS+POST 的在途窗口
            return await super().send_text(chat_id, text)

    class _Mgr:
        def __init__(self, channel):
            self.channels = {"feishu": channel}

    ch = _SlowChannel()

    async def enable():
        store = await SessionStore(db_path()).connect()
        try:
            await store.update_cron_task(cron_task["id"], notify_channel=1)
        finally:
            await store.close()

    asyncio.run(enable())
    monkeypatch.setattr(cli_app, "_build_sendonly_channels", lambda cfg: _Mgr(ch))
    _patch_provider(monkeypatch, FakeProvider([[TextBlock(text="慢推送：完成")]]))

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 0
    assert ch.sent, "main() 返回前在途推送必须已落地（退出前有界等待）"
    assert "每日汇总" in ch.sent[0][1]


def test_cron_run_cli_uses_line_model(home, cron_task, monkeypatch):
    """产线级模型在 CLI 的 cron run 同样生效：不能桌面跑 glm-4.6、CLI 跑全局。"""
    import skysheep.cli.app as cli_app

    async def set_model():
        store = await SessionStore(db_path()).connect()
        try:
            await store.update_cron_task(
                cron_task["id"], provider_name="zhipu", model="glm-4.6")
        finally:
            await store.close()

    asyncio.run(set_model())
    calls: list[tuple[str, str]] = []

    def _fake_build(name, pc, *a, **k):
        calls.append((str(name), str(getattr(pc, "model", ""))))
        return FakeProvider([[TextBlock(text="今日汇总：完成")]])

    monkeypatch.setattr(cli_app, "build_provider", _fake_build)
    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 0
    assert ("zhipu", "glm-4.6") in calls, "产线指定的服务 / 模型必须传给 build_provider"


def test_cron_run_cli_applies_review(home, cron_task, monkeypatch):
    """CLI 的 cron run 也执行验收：同一条任务不能桌面验收、CLI 只看自动判定。"""
    import skysheep.cli.app as cli_app

    async def enable_review():
        store = await SessionStore(db_path()).connect()
        try:
            await store.update_cron_task(cron_task["id"], review=1)
        finally:
            await store.close()

    asyncio.run(enable_review())
    # 第一组给主轮，第二组给验收（同一实例按序弹出）
    provider = FakeProvider([
        [TextBlock(text="今日汇总：完成")],
        [TextBlock(text='{"verdict":"failed","reason":"只有过程，没有结论"}')],
    ])
    monkeypatch.setattr(cli_app, "build_provider", lambda *a, **k: provider)
    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron", "run", str(cron_task["id"])])
    assert ei.value.code == 0

    async def read_row():
        store = await SessionStore(db_path()).connect()
        try:
            return (await store.list_automation_runs())[0]
        finally:
            await store.close()

    row = asyncio.run(read_row())
    assert row["status"] == "done"
    assert row["outcome"] == "failed" and row["outcome_source"] == "reviewer"
    assert "没有结论" in row["review_note"]
