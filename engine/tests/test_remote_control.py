"""远程控制加固（2026-09 第二轮）的回归测试。

覆盖四组行为：
1. 令牌守卫 fail closed：远程来源需要令牌而令牌为空（配置被手改坏）时必须
   拒绝——旧行为是「没令牌可比对就放行」，等于配置坏掉时裸奔。
2. TokenThrottle：同一来源连续试错要付出代价（即时节流），成功验证清零。
3. 令牌轮换 lan.rotate_token：立即生效（守卫每请求读最新配置），旧令牌作废，
   失败计数清零；且在 LOCAL_ONLY 门禁表里（远程不能换锁）。
4. 一键重启：_relaunch_command 三种启动形态都能构造等价命令；app.restart
   在 LOCAL_ONLY 表里，拉起新进程后走 restart_hook → request_shutdown 收尾。

审批 actor 绑定与渠道排队回执在 test_channels.py。
"""

from __future__ import annotations

import asyncio
import sys
import time

from test_server import make_client, recv_until  # noqa: F401

import skysheep.server.app as server_app
from skysheep.channels.gate import ChannelGate
from skysheep.config import update_config_section
from skysheep.security.gate import Decision
from skysheep.server.app import LOCAL_ONLY_METHODS
from skysheep.server.backend import ServerBackend, TokenThrottle
from skysheep.tools.base import Safety, Tool


class _WriteTool(Tool):
    name = "write_file"
    description = "写入文件"
    safety = Safety.WRITE

    async def run(self, args, ctx):  # pragma: no cover - 测试不执行
        return ""


# ---- 1. 令牌守卫 fail closed ----


def test_guard_fails_closed_when_lan_enabled_without_token(home, monkeypatch):
    """lan=true 但 token 为空：远程请求一律 403，不能退化成无鉴权放行。"""
    update_config_section("server", {"lan": True, "token": ""})
    monkeypatch.setattr(server_app, "client_origin", lambda c: "other")
    with make_client(home, []) as client:
        assert client.get("/").status_code == 403
        assert client.get("/health").status_code == 403


def test_guard_fails_closed_for_tailnet_without_token(home, monkeypatch):
    """仅远程访问模式同理：tailnet 来源 + 空令牌 = 拒绝，而不是放进来。"""
    update_config_section("server", {"tailscale": True, "token": ""})
    monkeypatch.setattr(server_app, "client_origin", lambda c: "tailscale")
    with make_client(home, []) as client:
        assert client.get("/", params={"token": "anything"}).status_code == 403


def test_local_still_exempt_when_token_empty(home):
    """fail closed 只针对远程来源：本机回环免令牌（桌面不能被挡在门外）。"""
    update_config_section("server", {"lan": True, "token": ""})
    with make_client(home, []) as client:
        assert client.get("/").status_code == 200


# ---- 2. TokenThrottle ----


def test_token_throttle_blocks_after_threshold():
    th = TokenThrottle(threshold=3, base_delay=30.0)
    now = 1000.0
    assert th.blocked("1.2.3.4", now=now) is False
    assert th.note_failure("1.2.3.4", now=now) == 0.0
    assert th.note_failure("1.2.3.4", now=now) == 0.0
    # 第 3 次失败触发封锁
    assert th.note_failure("1.2.3.4", now=now) == 30.0
    assert th.blocked("1.2.3.4", now=now) is True
    # 封锁期内调用方不再比对，封到期为止
    assert th.blocked("1.2.3.4", now=now + 29) is True
    assert th.blocked("1.2.3.4", now=now + 31) is False
    # 指数退避：再次触发时长翻倍
    assert th.note_failure("1.2.3.4", now=now + 31) == 60.0


def test_token_throttle_success_resets_and_isolates_by_ip():
    th = TokenThrottle(threshold=2, base_delay=30.0)
    th.note_failure("1.1.1.1", now=0.0)
    th.note_failure("1.1.1.1", now=0.0)
    assert th.blocked("1.1.1.1", now=1.0) is True
    # 别的来源不受牵连
    assert th.blocked("2.2.2.2", now=1.0) is False
    # 验证成功即清零（输错几次后输对了，不能把主人锁在门外）
    th.note_success("1.1.1.1")
    assert th.blocked("1.1.1.1", now=1.0) is False


def test_guard_logs_failures_into_status(home, monkeypatch):
    """失败要留痕进 lan_status：设置页能看见「谁在敲门」。"""
    update_config_section("server", {"lan": True, "token": "tok-123"})
    monkeypatch.setattr(server_app, "client_origin", lambda c: "other")
    with make_client(home, []) as client:
        assert client.get("/", params={"token": "wrong"}).status_code == 403
        be = client.app.state.backend
        assert be.token_failure_summary()["total"] == 1
        assert be.token_failure_summary()["recent"][0]["ip"]


# ---- 3. 令牌轮换 ----


def test_rotate_token_takes_effect_immediately(home, monkeypatch):
    """轮换不用重启：新令牌马上可用，旧令牌（含已种 cookie）立即作废。"""
    update_config_section("server", {"lan": True, "token": "tok-old-value"})
    monkeypatch.setattr(server_app, "client_origin", lambda c: "other")
    # client_origin 的补丁会连带把 dispatch 的本机判定翻成远端：设置页场景
    # 是「本机界面操作」，这里把本机判定固定回 True
    monkeypatch.setattr(server_app, "_client_is_local", lambda c: True)
    with make_client(home, []) as client, client.websocket_connect(
        "/ws?token=tok-old-value"
    ) as ws:
        ws.send_json({"id": "r1", "method": "lan.rotate_token"})
        frame = recv_until(ws, "r1")
        assert frame["ok"], frame
        new_token = frame["result"]["token"]
        assert new_token and new_token != "tok-old-value"
        # 同一进程内立即生效：新令牌放行，旧令牌拒绝
        assert client.get("/", params={"token": new_token}).status_code == 200
        assert client.get("/", params={"token": "tok-old-value"}).status_code == 403


def test_rotate_resets_failures_but_keeps_blocks(home):
    """换锁：失败计数清零，但已生效的封锁保留。

    安全审查低危项：旧实现直接换一个新 TokenThrottle，正在被封锁的爆破来源
    跟着一起解封——换了锁不等于要放人进来。已生效的封锁按原冷却时间走完；
    只是「攒了几次失败还没触发」的来源，随换锁从零开始（用户自己换了地址，
    旧的敲门记录不再有意义）。
    """
    with make_client(home, []) as client:
        be = client.app.state.backend
        # 攒失败但未达阈值：换锁后应从零开始
        for _ in range(4):
            be.token_throttle.note_failure("5.6.7.8")
        assert be.token_throttle.blocked("5.6.7.8") is False
        asyncio.run(be.lan_rotate_token())
        be.token_throttle.note_failure("5.6.7.8")  # 若计数没清，这一次就到阈值
        assert be.token_throttle.blocked("5.6.7.8") is False, "换锁应清掉失败计数"

        # 已触发封锁：换锁后仍然封着
        for _ in range(5):  # 默认阈值 5
            be.token_throttle.note_failure("1.2.3.4")
        assert be.token_throttle.blocked("1.2.3.4") is True
        asyncio.run(be.lan_rotate_token())
        assert be.token_throttle.blocked("1.2.3.4") is True, "已生效的封锁不该被换锁解掉"


def test_rotate_and_restart_are_local_only():
    """换令牌与重启都动本机安全姿态：必须在 LOCAL_ONLY 门禁表里。"""
    assert "lan.rotate_token" in LOCAL_ONLY_METHODS
    assert "app.restart" in LOCAL_ONLY_METHODS


# ---- 4. 一键重启 ----


def test_relaunch_command_frozen(monkeypatch):
    """打包态：重跑当前 exe，原样带上参数。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\App\SkySheep.exe", raising=False)
    monkeypatch.setattr(sys, "argv", [r"C:\App\SkySheep.exe"])
    assert ServerBackend._relaunch_command() == [r"C:\App\SkySheep.exe"]


def test_relaunch_command_script(monkeypatch, tmp_path):
    """脚本态（SkySheep.pyw）：同一解释器重跑同一脚本。"""
    script = tmp_path / "SkySheep.pyw"
    script.write_text("# launcher", encoding="utf-8")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Python\pythonw.exe", raising=False)
    monkeypatch.setattr(sys, "argv", [str(script)])
    assert ServerBackend._relaunch_command() == [r"C:\Python\pythonw.exe", str(script)]


def test_relaunch_command_dev_falls_back_to_console_entry(monkeypatch):
    """开发态：argv[0] 是 console script（不以 .py 结尾）时，用 -c 调 main()。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Venv\Scripts\python.exe", raising=False)
    monkeypatch.setattr(sys, "argv", [r"C:\Venv\Scripts\skysheep.exe", "app", "--port", "9"])
    cmd = ServerBackend._relaunch_command()
    assert cmd[:2] == [r"C:\Venv\Scripts\python.exe", "-c"]
    assert "main" in cmd[2]
    assert cmd[3:] == ["app", "--port", "9"]


def test_app_restart_spawns_then_shuts_down(home, monkeypatch):
    """重启 = 先拉起等价新进程，稍后走 restart_hook → request_shutdown 收尾。"""
    spawned: list[list[str]] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            spawned.append(list(cmd))

    monkeypatch.setattr("skysheep.server.backend.subprocess.Popen", _FakePopen)
    monkeypatch.setattr(ServerBackend, "_relaunch_command", staticmethod(lambda: ["noop"]))
    with make_client(home, []) as client:
        be = client.app.state.backend
        calls: list[str] = []
        be.restart_hook = lambda: calls.append("hook")
        be.request_shutdown = lambda: calls.append("shutdown")
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "rs1", "method": "app.restart"})
            frame = recv_until(ws, "rs1")
            assert frame["ok"] is True, frame
            assert spawned == [["noop"]]
        # 收尾是延迟任务（让回包先发出）：在 portal 线程的循环里等它跑完
        deadline = time.monotonic() + 5.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls == ["hook", "shutdown"]


# ---- 渠道审批的后端接线（gate/manager 层在 test_channels.py） ----


async def test_channel_submit_decision_reports_actor_mismatch(home, monkeypatch):
    """backend 层：actor 与发起人不一致时返回 actor_mismatch，决定不生效。"""
    with make_client(home, []) as client:
        be = client.app.state.backend

        async def fake_binding(name):
            return "sess-1"

        monkeypatch.setattr(be.store, "get_channel_binding", fake_binding)
        gate = ChannelGate(approve_enabled=True, approve_timeout=30)
        gate.turn_actor = "u-owner"
        be._channel_gates["sess-1"] = gate
        task = asyncio.ensure_future(gate.authorize(_WriteTool(), {"path": "x"}))
        await asyncio.sleep(0.05)  # 让 pending 进入 waiting
        assert gate.waiting, "pending 应已登记"

        res = await be.channel_submit_decision("feishu", "allow", actor="u-other")
        assert res == {"hit": False, "actor_mismatch": True}
        res = await be.channel_submit_decision(
            "feishu", Decision.ALLOW_ONCE, actor="u-owner"
        )
        assert res["hit"] is True
        pending = await asyncio.wait_for(task, timeout=5)
        assert await pending.wait() == Decision.ALLOW_ONCE
