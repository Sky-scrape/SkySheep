"""定时任务：store CRUD/next_run 计算、WS 协议、headless 权限门控。"""

from __future__ import annotations

import time as _time

from skysheep.messages import TextBlock, ToolUseBlock

# ---- store：CRUD + next_run ----


async def test_cron_store_crud_and_next_run(store):
    project = await store.get_or_create_project("/tmp/cron-proj")
    t = await store.add_cron_task(project.id, "汇总", "看看 TODO", "interval",
                                  interval_minutes=30)
    assert t["id"] > 0 and t["interval_minutes"] == 30
    tasks = await store.list_cron_tasks(project.id)
    assert len(tasks) == 1 and tasks[0]["name"] == "汇总"

    # next_run：interval 基于 last_run_at
    row = {**t, "last_run_at": 1000.0}
    assert abs(store.compute_next_run(row) - 1000.0 - 1800) < 1

    # daily：今天已过点 → 明天
    lt = _time.localtime()
    row_d = {**t, "schedule_type": "daily",
             "time_of_day": f"{lt.tm_hour}:{lt.tm_min:02d}",
             "last_run_at": 0}
    next_d = store.compute_next_run(row_d, now=_time.time())
    # 构造 now 让今天的时刻已过，得到的一定是 > now 且 < now+24h
    assert next_d > _time.time() and next_d - _time.time() < 24 * 3600 + 60

    # weekly 也落在未来
    row_w = {**t, "schedule_type": "weekly", "time_of_day": "09:00", "weekday": 0}
    assert store.compute_next_run(row_w) > _time.time()

    # update / delete
    up = await store.update_cron_task(t["id"], enabled=0, allowed_tools=["read_file"])
    assert up["enabled"] is False and up["allowed_tools"] == ["read_file"]
    assert await store.delete_cron_task(t["id"]) is True
    assert await store.get_cron_task(t["id"]) is None


async def test_due_cron_only_enabled_and_scheduled(store):
    project = await store.get_or_create_project("/tmp/cron-proj2")
    t = await store.add_cron_task(project.id, "到点", "干活", "interval", interval_minutes=10)
    # 未排期（next_run_at=0）不算到期
    assert await store.due_cron_tasks() == []
    await store.update_cron_task(t["id"], next_run_at=_time.time() - 5)
    due = await store.due_cron_tasks()
    assert len(due) == 1 and due[0]["id"] == t["id"]
    await store.update_cron_task(t["id"], enabled=0)
    assert await store.due_cron_tasks() == []


# ---- WS：创建 / 立即运行 / headless 权限门控 ----


def test_cron_add_list_update_via_ws(home):
    from test_server import make_client, recv_until

    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "每日报告", "prompt": "汇总今天的进展",
            "schedule_type": "daily", "time_of_day": "09:00",
            "allowed_tools": ["read_file"],
        }})
        r = recv_until(ws, "ca")
        assert r["ok"], r.get("error")
        task = r["result"]
        assert task["name"] == "每日报告" and task["enabled"] is True
        assert task["next_run_at"] > 0, "创建后应立刻排期"

        ws.send_json({"id": "cl", "method": "cron.list", "params": {}})
        lst = recv_until(ws, "cl")["result"]["tasks"]
        assert len(lst) == 1 and lst[0]["allowed_tools"] == ["read_file"]

        # 非法频率拒绝
        ws.send_json({"id": "bad", "method": "cron.add", "params": {
            "name": "x", "prompt": "y", "schedule_type": "monthly"}})
        bad = recv_until(ws, "bad")
        assert not bad["ok"]

        ws.send_json({"id": "cu", "method": "cron.update", "params": {
            "id": task["id"], "enabled": False}})
        up = recv_until(ws, "cu")["result"]
        assert up["enabled"] is False and up["next_run_at"] == 0, "停用后不排期"


def test_cron_run_now_headless_gate(home):
    """无人值守门控：未预授权的 write_file 被自动拒绝；预授权后放行。"""
    from pathlib import Path

    from test_server import make_client, recv_until

    script = [
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="结果一")],
        [ToolUseBlock(id="w2", name="write_file",
                      input={"path": "ok.txt", "content": "y"})],
        [TextBlock(text="结果二")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "无人值守", "prompt": "写个文件",
            "schedule_type": "interval", "interval_minutes": 10,
            "allowed_tools": [],
        }})
        task = recv_until(ws, "ca")["result"]

        # 运行 1：write_file 未预授权 → 拒绝，文件不产生
        ws.send_json({"id": "r1", "method": "cron.run_now", "params": {"id": task["id"]}})
        recv_until(ws, "r1")
        secret = Path(home) / "proj" / "secret.txt"
        assert not secret.exists(), "未预授权写入必须被 headless 门控拒绝"

        ws.send_json({"id": "gl", "method": "cron.list", "params": {}})
        lst = recv_until(ws, "gl")["result"]["tasks"]
        assert lst[0]["last_status"] == "ok"
        assert lst[0]["last_result"] == "结果一"
        assert lst[0]["next_run_at"] > _time.time() - 1, "运行后重算下次时间"

        # 预授权 write_file → 放行落盘
        ws.send_json({"id": "cu", "method": "cron.update", "params": {
            "id": task["id"], "allowed_tools": ["write_file"]}})
        recv_until(ws, "cu")
        ws.send_json({"id": "r2", "method": "cron.run_now", "params": {"id": task["id"]}})
        recv_until(ws, "r2")
        okf = Path(home) / "proj" / "ok.txt"
        assert okf.exists() and okf.read_text(encoding="utf-8") == "y"

        ws.send_json({"id": "gl2", "method": "cron.list", "params": {}})
        lst2 = recv_until(ws, "gl2")["result"]["tasks"]
        assert lst2[0]["last_result"] == "结果二"


# ---- 终态推送：跑完后经渠道 manager 推摘要（开+绑定才发；失败不碰任务结果） ----


class _FakeChannel:
    """渠道替身：推送路径用到的最小接口（enabled / configured / allowed_ids / send_text）。"""

    name = "feishu"

    def __init__(self, allowed=("owner-1",), *, enabled=True, fail=False):
        self.config = {"enabled": enabled, "allowed_ids": list(allowed)}
        self.sent: list[tuple[str, str]] = []
        self.failed = 0
        self._fail = fail

    def configured(self) -> bool:
        return True

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    @property
    def allowed_ids(self) -> set[str]:
        return {str(x) for x in self.config.get("allowed_ids") or []}

    async def send_text(self, chat_id: str, text: str) -> bool:
        if self._fail:
            self.failed += 1
            raise RuntimeError("网络不可用")
        self.sent.append((chat_id, text))
        return True


def _bind_channel(client, name: str, channel) -> None:
    """把假渠道塞进真实 ChannelManager 的注册表（测试配置里没有任何启用渠道）。"""
    client.app.state.backend.channels.channels[name] = channel


def _wait_until(cond, timeout: float = 5.0) -> bool:
    """轮询等后台推送落地（TestClient 在另一线程跑应用的事件循环）。"""
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if cond():
            return True
        _time.sleep(0.02)
    return False


async def test_cron_notify_channel_persist_default_off(store):
    """开关随任务行持久化：默认关（防打扰），可改可关。"""
    project = await store.get_or_create_project("/tmp/cron-proj-push")
    t = await store.add_cron_task(project.id, "推送", "干活", "interval",
                                  interval_minutes=10)
    assert t["notify_channel"] is False, "默认必须关（防打扰）"
    up = await store.update_cron_task(t["id"], notify_channel=1)
    assert up["notify_channel"] is True
    assert (await store.get_cron_task(t["id"]))["notify_channel"] is True
    assert (await store.update_cron_task(t["id"], notify_channel=0))["notify_channel"] is False


async def test_cron_notify_channel_legacy_db_migration(tmp_path):
    """旧库没有 notify_channel 列：connect 补列，既有任务默认关、可再打开。"""
    import sqlite3

    from skysheep.session.store import SessionStore

    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE cron_tasks ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER,"
        " name TEXT NOT NULL,"
        " prompt TEXT NOT NULL,"
        " schedule_type TEXT NOT NULL DEFAULT 'interval',"
        " interval_minutes INTEGER NOT NULL DEFAULT 0,"
        " time_of_day TEXT NOT NULL DEFAULT '',"
        " weekday INTEGER NOT NULL DEFAULT -1,"
        " allowed_tools TEXT NOT NULL DEFAULT '[]',"
        " enabled INTEGER NOT NULL DEFAULT 1,"
        " last_run_at REAL NOT NULL DEFAULT 0,"
        " last_status TEXT NOT NULL DEFAULT '',"
        " last_result TEXT NOT NULL DEFAULT '',"
        " next_run_at REAL NOT NULL DEFAULT 0,"
        " created_at REAL NOT NULL)"
    )
    con.execute(
        "INSERT INTO cron_tasks (project_id, name, prompt, schedule_type,"
        " interval_minutes, created_at) VALUES (1, '旧任务', '干活', 'interval', 30, 0)"
    )
    con.commit()
    con.close()

    s = await SessionStore(db).connect()
    try:
        tasks = await s.list_cron_tasks()
        assert len(tasks) == 1 and tasks[0]["name"] == "旧任务"
        assert tasks[0]["notify_channel"] is False, "迁移进来的旧任务默认关"
        up = await s.update_cron_task(tasks[0]["id"], notify_channel=1)
        assert up["notify_channel"] is True
    finally:
        await s.close()


def test_cron_finish_pushes_summary_to_bound_channel(home):
    """开关开 + 渠道启用且名单非空 → 终态推一条紧凑摘要到允许名单。"""
    from test_server import make_client, recv_until

    with make_client(home, [[TextBlock(text="今日汇总：完成三件事")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ch = _FakeChannel()
        _bind_channel(client, "feishu", ch)

        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "每日汇总", "prompt": "汇总进展",
            "schedule_type": "interval", "interval_minutes": 30,
            "notify_channel": True,
        }})
        task = recv_until(ws, "ca")["result"]
        assert task["notify_channel"] is True, "创建时的开关要落库"

        ws.send_json({"id": "r1", "method": "cron.run_now", "params": {"id": task["id"]}})
        r = recv_until(ws, "r1")
        assert r["ok"], r.get("error")

        assert _wait_until(lambda: len(ch.sent) == 1), f"应推送一条摘要，实际 {ch.sent}"
        chat_id, text = ch.sent[0]
        assert chat_id == "owner-1", "推送目标是渠道允许名单（主人准入标识）"
        assert "每日汇总" in text and "成功" in text
        assert "耗时" in text and "完成时间" in text
        assert "今日汇总：完成三件事" in text
        assert len(text) < 2000, "摘要必须紧凑（结果段≤500 字符）"

        # 任务行照常写回：推送是附赠动作，不影响主流程
        ws.send_json({"id": "gl", "method": "cron.list", "params": {}})
        lst = recv_until(ws, "gl")["result"]["tasks"]
        assert lst[0]["last_status"] == "ok"


def test_cron_finish_push_silent_when_off_or_unbound(home):
    """开关关（默认）→ 一个字不发；开关开但渠道未启用/名单为空 → 静默跳过。"""
    from test_server import make_client, recv_until

    with make_client(home, [[TextBlock(text="结果A")], [TextBlock(text="结果B")]]) as client, \
            client.websocket_connect("/ws") as ws:
        off = _FakeChannel()                 # 名单正常，但任务开关关
        disabled = _FakeChannel(enabled=False)  # 渠道未启用
        unbound = _FakeChannel(allowed=())   # 渠道启用但名单为空（没绑定聊天）
        _bind_channel(client, "feishu", off)
        _bind_channel(client, "weixin", disabled)

        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "不开推送", "prompt": "干活",
            "schedule_type": "interval", "interval_minutes": 30,
        }})
        t1 = recv_until(ws, "ca")["result"]
        assert t1["notify_channel"] is False, "不传开关 = 默认关"

        ws.send_json({"id": "r1", "method": "cron.run_now", "params": {"id": t1["id"]}})
        recv_until(ws, "r1")
        ws.send_json({"id": "gl", "method": "cron.list", "params": {}})
        assert recv_until(ws, "gl")["result"]["tasks"][0]["last_status"] == "ok"
        _time.sleep(0.3)  # 留出后台推送窗口：真发了早就到了
        assert off.sent == [], "开关关（默认）不得推送"
        assert disabled.sent == [], "渠道未启用不得推送"

        # 开关打开，但 feishu 换成名单为空的渠道：仍然无目标可发
        _bind_channel(client, "feishu", unbound)
        ws.send_json({"id": "cb", "method": "cron.update", "params": {
            "id": t1["id"], "notify_channel": True}})
        assert recv_until(ws, "cb")["result"]["notify_channel"] is True
        ws.send_json({"id": "r2", "method": "cron.run_now", "params": {"id": t1["id"]}})
        recv_until(ws, "r2")
        _time.sleep(0.3)
        assert unbound.sent == [], "名单为空（未绑定聊天）静默跳过"
        assert unbound.failed == 0 and disabled.failed == 0, "静默跳过不产生发送尝试"


def test_cron_push_failure_does_not_affect_task_result(home, caplog):
    """推送发送失败只记日志：任务状态/结果/下次排期照常落库。"""
    import logging

    from test_server import make_client, recv_until

    with make_client(home, [[TextBlock(text="成功产出")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ch = _FakeChannel(fail=True)
        _bind_channel(client, "feishu", ch)

        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "推送会炸", "prompt": "干活",
            "schedule_type": "interval", "interval_minutes": 30,
            "notify_channel": True,
        }})
        task = recv_until(ws, "ca")["result"]

        ws.send_json({"id": "r1", "method": "cron.run_now", "params": {"id": task["id"]}})
        r = recv_until(ws, "r1")
        assert r["ok"], r.get("error")

        assert _wait_until(lambda: ch.failed >= 1), "推送应已尝试发送"
        ws.send_json({"id": "gl", "method": "cron.list", "params": {}})
        row = recv_until(ws, "gl")["result"]["tasks"][0]
        assert row["last_status"] == "ok", "推送失败不得改写任务状态"
        assert row["last_result"] == "成功产出", "任务结果不受推送失败影响"
        assert row["next_run_at"] > _time.time() - 5, "下次运行时间照常重算"

        hits = [rec for rec in caplog.records
                if "定时任务摘要推送" in rec.getMessage()]
        assert hits, "推送失败要留日志（skysheep.security）"
        assert all(rec.levelno >= logging.WARNING for rec in hits)