

# ---- P22：Tailscale 一键配置向导的探测 ----


async def test_tailscale_probe_reports_missing_client(home, monkeypatch):
    """没装客户端：step=install + 下载链接，不去猜别的。"""
    from types import SimpleNamespace

    from skysheep.server.backend_parts import remote as remote_mod
    from skysheep.server.backend_parts.remote import RemoteMixin

    monkeypatch.setattr(remote_mod.shutil, "which", lambda *a, **k: None)

    class _Host(RemoteMixin):
        pass

    host = _Host()
    host.cfg = SimpleNamespace(server=SimpleNamespace(tailscale=False, token="", port=0))
    out = await host.remote_tailscale_probe()
    assert out["installed"] is False and out["step"] == "install"
    assert "tailscale.com" in out["download"]
    assert "Tailscale" in out["hint"]


async def test_tailscale_probe_guides_login_when_cli_fails(home, monkeypatch):
    """CLI 在但连不上（没登录/服务没起）：step=login，URL 留空。"""
    from types import SimpleNamespace

    from skysheep.server.backend_parts import remote as remote_mod
    from skysheep.server.backend_parts.remote import RemoteMixin

    monkeypatch.setattr(remote_mod.shutil, "which", lambda *a, **k: "C:/nowhere/tailscale.exe")

    class _Host(RemoteMixin):
        pass

    host = _Host()
    host.cfg = SimpleNamespace(server=SimpleNamespace(tailscale=True, token="tok", port=8899))
    out = await host.remote_tailscale_probe()
    assert out["installed"] is True and out["running"] is False
    assert out["step"] == "login" and out["url"] == ""
    assert "登录" in out["hint"]


async def test_tailscale_probe_timeout_reaps_child_process(home, monkeypatch):
    """探测超时要收割子进程（kill + wait 收尸）：孤儿进程不能随每次点击堆积。

    用真子进程（sleep 30s 的解释器）冒充卡死的 `tailscale status`：wait_for
    超时后必须看到它被杀掉（returncode 落定），探测本身仍按超时口径出
    step=login。杀完不收尸（只 kill 不 wait）或完全不 kill 都会被断言抓住。
    """
    import asyncio
    import sys
    from types import SimpleNamespace

    from skysheep.server.backend_parts import remote as remote_mod
    from skysheep.server.backend_parts.remote import RemoteMixin

    monkeypatch.setattr(remote_mod.shutil, "which", lambda *a, **k: "C:/nowhere/tailscale.exe")
    monkeypatch.setattr(remote_mod, "TAILSCALE_PROBE_TIMEOUT_S", 0.2)

    slow = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )

    async def fake_exec(*args, **kwargs):
        return slow  # 探测拿到的就是上面这个真子进程

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    class _Host(RemoteMixin):
        pass

    host = _Host()
    host.cfg = SimpleNamespace(server=SimpleNamespace(tailscale=True, token="tok", port=8899))
    reaped = False
    try:
        out = await host.remote_tailscale_probe()
        reaped = slow.returncode is not None
    finally:
        if slow.returncode is None:  # 兜底清理：失败用例不能往套件里漏 30 秒进程
            slow.kill()
            await slow.wait()
    assert out["running"] is False and out["step"] == "login"
    assert "登录" in out["hint"]
    assert reaped, "超时后子进程应被 kill + wait 收割，而不是留成孤儿"
