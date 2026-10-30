

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
