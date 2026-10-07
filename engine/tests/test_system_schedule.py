"""系统级定时调度（core/system_schedule.py + cron-run 子命令 + 后端挂钩）。

覆盖：schtasks 参数纯映射（分钟/小时/每天/每周参数化）、register/unregister
经 monkeypatch 的 subprocess 成败、`skysheep cron-run <id> --project <dir>`
小端到端（SKYSHEEP_HOME 隔离 + fake provider，断言任务行回写）、后端
cron.add/update/delete 的自动同步挂钩（注入假 register/unregister）。
绝不真实注册系统计划任务、绝不联网。
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from skysheep.config import db_path, load_config
from skysheep.core import system_schedule
from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.session import SessionStore

PY = r"C:\Program Files\Python311\python.exe"  # 故意带空格，验证 /TR 内引号
PROJ = r"D:\work\我的 项目"  # 故意带空格与非 ASCII


def _spec(**kw) -> dict:
    spec = {"schedule_type": "interval", "interval_minutes": 30,
            "time_of_day": "", "weekday": -1}
    spec.update(kw)
    return spec


# ---- build_schtasks_args：纯映射（参数化：分钟 / 小时 / 每天 / 每周） ----


@pytest.mark.parametrize(
    "spec, expect",
    [
        # 分钟：interval 非 60 倍数 → /SC MINUTE /MO n
        (_spec(interval_minutes=5), ["/SC", "MINUTE", "/MO", "5"]),
        (_spec(interval_minutes=45), ["/SC", "MINUTE", "/MO", "45"]),
        # 小时：interval 是 60 的整数倍 → /SC HOURLY /MO n
        (_spec(interval_minutes=60), ["/SC", "HOURLY", "/MO", "1"]),
        (_spec(interval_minutes=120), ["/SC", "HOURLY", "/MO", "2"]),
        # 每天：/SC DAILY /ST hh:mm
        (_spec(schedule_type="daily", time_of_day="08:30"),
         ["/SC", "DAILY", "/ST", "08:30"]),
        # 每周：weekday 0=周一..6=周日 → 三字母缩写
        (_spec(schedule_type="weekly", weekday=0, time_of_day="09:05"),
         ["/SC", "WEEKLY", "/D", "MON", "/ST", "09:05"]),
        (_spec(schedule_type="weekly", weekday=6),
         ["/SC", "WEEKLY", "/D", "SUN", "/ST", "09:00"]),
    ],
)
def test_build_schtasks_args_schedule_switches(spec, expect):
    args = system_schedule.build_schtasks_args(7, spec, PROJ, PY)
    assert args[:4] == ["/Create", "/F", "/TN", "SkySheepCron-7"]
    i = args.index("/SC")
    assert args[i:i + len(expect)] == expect
    assert args[-2] == "/TR"
    assert args[-1] == (f'"{PY}" -m skysheep.cli.app cron-run 7'
                        f' --project "{PROJ}"'), "入口与目录带空格要靠内引号保护"


def test_build_schtasks_args_daily_defaults_and_invalid_weekday():
    # time_of_day 缺省 09:00（与手动导出同口径）
    args = system_schedule.build_schtasks_args(
        1, _spec(schedule_type="daily", time_of_day=""), PROJ, PY)
    i = args.index("/SC")
    assert args[i:i + 4] == ["/SC", "DAILY", "/ST", "09:00"]

    # weekly 的无效 weekday：拒绝而非降级 DAILY（降级会把频次放大 7 倍）
    with pytest.raises(ValueError):
        system_schedule.build_schtasks_args(
            1, _spec(schedule_type="weekly", weekday=9), PROJ, PY)


# ---- register / unregister：monkeypatch subprocess 测成败 ----


class _Done:  # subprocess.run 返回的最小替代（CompletedProcess 面向鸭子类型）
    def __init__(self, returncode, out=b""):
        self.returncode = returncode
        self.stdout = out
        self.stderr = b""


def test_register_success_builds_create_command(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(list(argv))
        return _Done(0, "成功: 计划任务已创建。".encode("gbk"))

    monkeypatch.setattr(system_schedule.subprocess, "run", fake_run)
    ok, out = system_schedule.register(3, _spec(interval_minutes=15), PROJ, PY)
    assert ok and "计划任务已创建" in out, "GBK 输出也要能解回来"
    argv = calls[0]
    assert argv[0] == "schtasks" and argv[1] == "/Create"
    assert "/TN" in argv and "SkySheepCron-3" in argv and "/F" in argv


def test_unregister_success_and_failure(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(list(argv))
        if len(calls) == 1:
            return _Done(0, b"SUCCESS")
        return _Done(1, "错误: 系统找不到指定的文件。".encode("gbk"))

    monkeypatch.setattr(system_schedule.subprocess, "run", fake_run)
    ok, _ = system_schedule.unregister(9)
    assert ok
    assert calls[0] == ["schtasks", "/Delete", "/TN", "SkySheepCron-9", "/F"]

    ok, out = system_schedule.unregister(9)
    assert not ok and "找不到" in out, "schtasks 非零退出如实报失败"


def test_register_maps_build_error_without_schtasks(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("参数映射失败时不得调用 schtasks")

    monkeypatch.setattr(system_schedule.subprocess, "run", boom)
    ok, out = system_schedule.register(
        1, _spec(schedule_type="weekly", weekday=-1), PROJ, PY)
    assert not ok and "星期" in out


def test_register_reports_missing_or_timeout_schtasks(monkeypatch):
    def no_schtasks(*_a, **_k):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(system_schedule.subprocess, "run", no_schtasks)
    ok, out = system_schedule.unregister(1)
    assert not ok and "不可用" in out

    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="schtasks", timeout=1)

    monkeypatch.setattr(system_schedule.subprocess, "run", slow)
    ok, out = system_schedule.unregister(1)
    assert not ok and "超时" in out


# ---- cron-run 子命令：小端到端（SKYSHEEP_HOME 隔离 + fake provider） ----


@pytest.fixture
async def seeded_task(home):
    store = await SessionStore(db_path()).connect()
    try:
        proj = await store.get_or_create_project(str(home / "proj"))
        task = await store.add_cron_task(
            proj.id, "系统级每日汇总", "汇总一下", "daily", time_of_day="08:00")
        return task
    finally:
        await store.close()


def test_cron_run_subcommand_writes_back_status(home, seeded_task, monkeypatch):
    """`skysheep cron-run <id> --project <dir>`：fake provider 跑一轮并回写
    last_status / last_run_at（系统计划任务到点拉起的完整路径）。"""
    import skysheep.cli.app as cli_app

    provider = FakeProvider([[TextBlock(text="系统级调度：完成")]])
    monkeypatch.setattr(cli_app, "build_provider", lambda name, pc: provider)

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron-run", str(seeded_task["id"]),
                      "--project", str(home / "proj")])
    assert ei.value.code == 0
    assert provider.calls, "fake provider 应被执行"

    async def check():
        store = await SessionStore(db_path()).connect()
        try:
            row = await store.get_cron_task(seeded_task["id"])
            assert row["last_status"] == "ok"
            assert row["last_run_at"] > 0, "完成后更新 last_run"
            assert "完成" in (row["last_result"] or "")
        finally:
            await store.close()

    asyncio.run(check())


def test_cron_run_rejects_missing_project_dir(home, seeded_task, monkeypatch):
    """--project 指向不存在的目录：拒绝执行，不产生模型调用。"""
    import skysheep.cli.app as cli_app

    provider = FakeProvider([[TextBlock(text="不该出现")]])
    monkeypatch.setattr(cli_app, "build_provider", lambda name, pc: provider)

    with pytest.raises(SystemExit) as ei:
        cli_app.main(["cron-run", str(seeded_task["id"]),
                      "--project", str(home / "no-such-dir")])
    assert "不存在" in str(ei.value.code)
    assert provider.calls == []


# ---- config：[cron] system_schedule 默认关、可从 config.toml 读回 ----


def test_cron_system_schedule_default_off_and_parse(home, monkeypatch):
    assert load_config().cron.system_schedule is False, "默认必须关"

    cfg_file = home / "home" / "config.toml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text("[cron]\nsystem_schedule = true\n", encoding="utf-8")
    assert load_config().cron.system_schedule is True


# ---- 后端挂钩：cron.add / update / delete 自动同步（注入假 register/unregister） ----


def _wire_fakes(monkeypatch):
    reg, unreg = [], []

    def fake_register(task_id, spec, project_dir, engine_python):
        reg.append((task_id, dict(spec), project_dir, engine_python))
        return True, "SUCCESS"

    def fake_unregister(task_id):
        unreg.append(task_id)
        return True, "SUCCESS"

    monkeypatch.setattr(system_schedule, "register", fake_register)
    monkeypatch.setattr(system_schedule, "unregister", fake_unregister)
    return reg, unreg


def test_backend_auto_sync_on_cron_add_update_delete(home, monkeypatch):
    """开关开：新建注册 → 停用注销 → 删除注销；参数来自任务行与项目目录。"""
    from test_server import make_client, recv_until

    reg, unreg = _wire_fakes(monkeypatch)

    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        client.app.state.backend.cfg.cron.system_schedule = True

        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "自动注册", "prompt": "干活",
            "schedule_type": "interval", "interval_minutes": 15}})
        task = recv_until(ws, "ca")["result"]
        assert task["id"] > 0
        assert len(reg) == 1 and reg[0][0] == task["id"]
        assert reg[0][1]["interval_minutes"] == 15, "cron_spec 来自任务行"
        assert reg[0][3] == system_schedule.default_engine_python()[0]

        # 停用 → 注销
        ws.send_json({"id": "cu", "method": "cron.update",
                      "params": {"id": task["id"], "enabled": False}})
        recv_until(ws, "cu")
        assert unreg == [task["id"]]

        # 重新启用 → 重建（register 再次出现）
        ws.send_json({"id": "ce", "method": "cron.update",
                      "params": {"id": task["id"], "enabled": True}})
        recv_until(ws, "ce")
        assert len(reg) == 2 and reg[-1][0] == task["id"]

        # 删除 → 注销
        ws.send_json({"id": "cd", "method": "cron.delete",
                      "params": {"id": task["id"]}})
        recv_until(ws, "cd")
        assert unreg.count(task["id"]) == 2


def test_backend_auto_sync_off_by_default(home, monkeypatch):
    """开关默认关：cron.add 成功但不碰 schtasks。"""
    from test_server import make_client, recv_until

    def no_register(*_a, **_k):
        raise AssertionError("开关关闭时不得注册系统计划任务")

    def no_unregister(*_a, **_k):
        raise AssertionError("开关关闭时不得注销系统计划任务")

    monkeypatch.setattr(system_schedule, "register", no_register)
    monkeypatch.setattr(system_schedule, "unregister", no_unregister)

    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "默认不同步", "prompt": "干活",
            "schedule_type": "daily", "time_of_day": "10:00"}})
        r = recv_until(ws, "ca")
        assert r["ok"], r.get("error")
